"""Generic OpenSSH connector that carries the originating Access Client."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from .access_context import AccessContext, encode_access_context, platform_name
from .clipboard import native_clipboard_provider
from .clipboard_bridge import ClipboardBridgeServer, _create_token_state


def _valid_target(target: str) -> bool:
    return bool(
        isinstance(target, str)
        and target
        and len(target) <= 255
        and not target.startswith("-")
        and all(ch.isprintable() and not ch.isspace() for ch in target)
    )


def resolve_ssh_target(
    target: str,
    *,
    run: Optional[Callable] = None,
) -> Optional[dict]:
    """Read OpenSSH's effective config; never rebuild alias resolution."""

    if not _valid_target(target):
        return None
    runner = run or subprocess.run
    try:
        result = runner(
            ["ssh", "-G", target], capture_output=True, text=True, timeout=8, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    config = {}
    for line in (result.stdout or "").splitlines():
        key, _, value = line.partition(" ")
        if key in {"hostname", "user", "port", "proxyjump", "proxycommand"}:
            config[key] = value.strip()
    return config if config.get("hostname") else None


detect_clipboard_provider = native_clipboard_provider


def make_access_context(target: str, provider: str, *, now: Optional[float] = None) -> AccessContext:
    """Bind a remote Tower to this device, never to the SSH target identity."""

    host = socket.gethostname() or "UNKNOWN"
    try:
        tty = os.ttyname(0) if os.isatty(0) else ""
    except OSError:
        tty = ""
    client_id = hashlib.sha256(
        f"{host}\0{tty}\0{secrets.token_hex(16)}".encode("utf-8", "replace")
    ).hexdigest()[:32]
    return AccessContext(
        client_id=client_id,
        display_name=host,
        platform=platform_name(),
        transport="ssh",
        access_host=host,
        tower_host=target,
        clipboard_capability="bridge" if provider else "none",
        registration_timestamp=time.time() if now is None else now,
        liveness="live",
    )


def _open_reverse_tunnel(target: str, remote_port: int, local_port: int):
    process = subprocess.Popen(
        [
            "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=8", "-R",
            f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}", target,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        return process
    error = process.communicate()[1].decode("utf-8", "replace")[-500:]
    raise RuntimeError(error.strip() or "reverse SSH forwarding failed")


def _remote_bridge_command(target: str, action: str, client_id: str, data=None) -> bool:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target,
            "tower", "connect", f"--_{action}-client", client_id]
    payload = json.dumps(data, separators=(",", ":")).encode("ascii") if data is not None else None
    try:
        result = subprocess.run(
            argv, input=payload, capture_output=True, timeout=12, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def select_tower_session(active_sessions, requested: str = "") -> str:
    """Choose only one session with a live Tower registration."""

    active = tuple(dict.fromkeys(active_sessions))
    if requested:
        return requested if requested in active else ""
    return active[0] if len(active) == 1 else ""


def resolve_active_tower_session(requested: str = "") -> int:
    """Remote-only read-only probe; stale registrations are left untouched."""

    from .tmux import capture, registration

    sessions = capture.run_tmux(["list-sessions", "-F", "#{session_name}"]).splitlines()
    active = []
    for session in sessions:
        if requested and session != requested:
            continue
        pane_id = capture.get_session_option(session, registration.PANE_OPTION)
        if not pane_id or not capture.pane_is_live_in_session(pane_id, session):
            continue
        pid = capture.get_session_option(session, registration.PID_OPTION)
        if pid and (
            not registration.pid_alive(pid)
            or not registration.process_belongs_to_pane(pid, registration._pane_shell_pid(pane_id))
        ):
            continue
        active.append(session)
    selected = select_tower_session(active, requested)
    if not selected:
        return 2
    print(json.dumps(selected, ensure_ascii=True))
    return 0


def _find_remote_tower_session(target: str, requested: str = "") -> str:
    if requested and (
        len(requested) > 128 or any(not ch.isprintable() or ch in "\r\n" for ch in requested)
    ):
        return ""
    remote = ["tower", "connect", "--_resolve-session"]
    if requested:
        remote.append(requested)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, shlex.join(remote)],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    for line in reversed((result.stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value and len(value) <= 128 and all(
            ch.isprintable() and ch not in "\r\n" for ch in value
        ):
            return value
    return ""


def _close_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def connect(target: str, session: str = "") -> int:
    config = resolve_ssh_target(target)
    if config is None:
        print("연결 대상을 찾을 수 없습니다.", file=sys.stderr)
        return 2
    try:
        probe = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target, "true"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("연결 대상을 찾을 수 없습니다.", file=sys.stderr)
        return 2
    if probe.returncode != 0:
        print("연결 대상을 찾을 수 없습니다.", file=sys.stderr)
        return 2
    session = _find_remote_tower_session(target, session)
    if not session:
        print("활성 Tower 세션을 하나로 확인할 수 없습니다.", file=sys.stderr)
        return 2

    provider = detect_clipboard_provider()
    context = make_access_context(config.get("hostname", target), provider)
    instance_id = context.client_id
    state_dir = None
    bridge = None
    thread = None
    tunnel = None
    registered = False
    try:
        if provider:
            state_dir, _token_file, token = _create_token_state()
            bridge = ClipboardBridgeServer(token)
            thread = threading.Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            local_port = int(bridge.server_address[1])
            for _attempt in range(8):
                remote_port = secrets.randbelow(25000) + 35000
                try:
                    tunnel = _open_reverse_tunnel(target, remote_port, local_port)
                    break
                except RuntimeError:
                    continue
            if tunnel is None:
                raise RuntimeError("could not open a loopback clipboard connection")
            if not _remote_bridge_command(
                target, "register", instance_id,
                {"port": remote_port, "token": token, "access_context": context.__dict__},
            ):
                raise RuntimeError("remote Tower clipboard registration failed")
            registered = True

        environment = [
            f"TOWER_ACCESS_CONTEXT_V1={encode_access_context(context)}",
        ]
        if registered:
            environment.append(f"TOWER_CLIPBOARD_BRIDGE_ID={instance_id}")
        remote = ["env", *environment, "tmux", "attach-session", "-t", session]
        return subprocess.call(["ssh", "-tt", target, shlex.join(remote)])
    except (OSError, RuntimeError) as exc:
        print(f"Tower 연결 실패: {exc}", file=sys.stderr)
        return 1
    finally:
        if registered:
            _remote_bridge_command(target, "unregister", instance_id)
        _close_process(tunnel)
        if bridge is not None:
            bridge.shutdown()
            bridge.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if state_dir is not None:
            shutil.rmtree(state_dir, ignore_errors=True)


def register_from_stdin(client_id: str, *, unregister: bool = False) -> int:
    """Private SSH endpoint used by ``tower connect``; accepts only bridge metadata."""

    from .clipboard_bridge import main as bridge_main

    if not all(ch in "0123456789abcdef" for ch in client_id) or len(client_id) != 32:
        return 2
    return bridge_main(["--unregister-client" if unregister else "--register-client", client_id])
