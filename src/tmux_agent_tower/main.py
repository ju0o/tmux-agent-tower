"""``tower`` CLI entry point."""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import subprocess
import sys
import threading
import time

from . import __version__
from .i18n import t
from .server import httpapi, netutil, tailscale
from .tmux import capture as tmux_capture
from .tmux import registration
from .ui.tower import run as run_ui, load_remote_hosts

DEFAULT_SERVE_PORT = 4312


def _print_result(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    mark = "✓" if ok else ("!" if warn else "✗")
    status_word = t("doctor.ok") if ok else detail or t("doctor.fail")
    line = f"{mark} {label:<22} {status_word}"
    print(line)
    return ok


def run_doctor() -> int:
    print(t("doctor.title"))
    print()

    problems = 0

    tmux_path = shutil.which("tmux")
    if not _print_result(t("doctor.check.tmux_installed"), bool(tmux_path)):
        problems += 1

    if tmux_path:
        try:
            version = subprocess.run(
                ["tmux", "-V"], capture_output=True, text=True, timeout=3, check=False
            ).stdout.strip()
        except Exception:
            version = ""
        if not _print_result(t("doctor.check.tmux_version"), bool(version), version):
            problems += 1

    if not _print_result(
        t("doctor.check.python_version"),
        sys.version_info >= (3, 9),
        f"{sys.version_info.major}.{sys.version_info.minor}",
    ):
        problems += 1

    try:
        import curses as _curses  # noqa: F401

        _print_result(t("doctor.check.curses"), True)
    except Exception as exc:
        _print_result(t("doctor.check.curses"), False, str(exc))
        problems += 1

    term = os.environ.get("TERM", "")
    if term:
        _print_result(t("doctor.check.term"), True)
    else:
        _print_result(t("doctor.check.term"), False, t("doctor.term_missing"), warn=True)
        problems += 1

    session = tmux_capture.current_session()
    if session:
        _print_result(t("doctor.check.in_tmux"), True)
    else:
        _print_result(t("doctor.check.in_tmux"), False, t("doctor.not_in_tmux"), warn=True)
        problems += 1

    panes_output = tmux_capture.run_tmux(["list-panes", "-a"])
    if not _print_result(t("doctor.check.server"), bool(panes_output) or session != ""):
        problems += 1

    hosts = load_remote_hosts()
    print()
    if hosts:
        print(t("doctor.remote_hosts_configured", n=len(hosts), hosts=", ".join(h["alias"] for h in hosts)))
    else:
        print(t("doctor.remote_hosts_none"))

    print()
    print(t("doctor.summary_ok") if problems == 0 else t("doctor.summary_fail", n=problems))

    return 0 if problems == 0 else 1


def focus_active_tower() -> None:
    """Jump to whichever pane is currently registered as the active Tower
    for this session -- does NOT start a new Tower. Meant for a
    ``Ctrl+b w``-style binding (``run-shell -b "tower --focus"``): plain
    tmux navigation commands only, no curses, so it's safe to run
    backgrounded from a key binding.

    Never falls back to matching a window by name (see
    ``tmux/registration.py``'s module docstring for the bug that caused).
    """

    session = tmux_capture.current_session()
    if not session:
        print(t("cli.no_tmux_session"), file=sys.stderr)
        raise SystemExit(1)

    pane_id = registration.resolve_active_pane(session)
    if not pane_id:
        tmux_capture.display_message(t("cli.no_active_tower"))
        return

    registration.focus_pane(pane_id)


def run_serve(lan: bool, port: int, use_tailscale: bool = False) -> None:
    """``tower serve``: Tower Remote's tiny read/prompt-send HTTP API +
    mobile web UI (see ``server/httpapi.py`` for the security boundary).

    Binds to localhost only unless ``--lan`` is explicitly given -- never
    LAN-reachable by default. ``--tailscale`` is a third, separate mode:
    the backend still binds localhost-only (never ``0.0.0.0``), and a
    Windows-host ``tailscale serve`` mapping makes it reachable over your
    tailnet instead -- see ``server/tailscale.py`` and docs/REMOTE.md.
    """

    session = tmux_capture.current_session()
    if not session:
        print(t("cli.no_tmux_session"), file=sys.stderr)
        raise SystemExit(1)

    own_pane_id = tmux_capture.current_pane_id()

    host = "0.0.0.0" if (lan and not use_tailscale) else "127.0.0.1"
    allowed_hosts = ["localhost", "127.0.0.1"]
    lan_ip = netutil.detect_lan_ip() if (lan and not use_tailscale) else None
    if lan_ip:
        allowed_hosts.append(lan_ip)

    try:
        server = httpapi.create_server(host, port, session, own_pane_id, allowed_hosts)
    except OSError as exc:
        print(f"Could not start Tower Remote on port {port}: {exc}", file=sys.stderr)
        raise SystemExit(1)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    tailscale_exe = None
    tailscale_dns_name = None
    if use_tailscale:
        tailscale_exe, tailscale_dns_name = _setup_tailscale(server, port)
        if tailscale_exe is None:
            server.shutdown()
            server.server_close()
            raise SystemExit(1)

    print("TMUX AGENT TOWER REMOTE")
    print()

    if use_tailscale:
        print("Tailscale: ONLINE")
        print(f"https://{tailscale_dns_name}/")
    else:
        print(f"Local:  http://127.0.0.1:{port}")
        if lan:
            if lan_ip:
                print(f"LAN:    http://{lan_ip}:{port}")
            else:
                print("LAN:    (could not detect a LAN IP address on this machine)")
        else:
            print("(localhost only -- pass --lan to allow your phone to connect)")

    print()
    code = server.pairing.current_code()
    print(f"Pairing code: {code}  (valid 5 minutes, one-time use)")
    print("Open the address above on your phone's browser and enter this code.")
    print()
    print("Type 'r' + Enter any time for a new pairing code. Ctrl+C to stop.")
    sys.stdout.flush()

    stdin_thread = threading.Thread(target=_watch_stdin_for_regenerate, args=(server,), daemon=True)
    stdin_thread.start()

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print("Stopping Tower Remote...")
    finally:
        if use_tailscale and tailscale_exe:
            if tailscale.stop_serve(tailscale_exe, port):
                print("Tailscale Serve mapping removed.")
            else:
                # Either the mapping changed underneath us (so it isn't ours
                # to remove) or the CLI call failed -- never escalate to
                # `serve reset`; tell the user exactly what to run instead.
                print(
                    "Tailscale Serve mapping was left in place (it no longer points at this backend, "
                    "or removal failed). To remove it by hand: tailscale serve --https=443 off",
                    file=sys.stderr,
                )
        server.shutdown()
        server.server_close()


_TAILSCALE_ERROR_MESSAGES = {
    "tailscale_status_unavailable": "Could not reach the Windows Tailscale daemon (is Tailscale running and logged in?).",
    "no_magicdns": "MagicDNS is not enabled for this tailnet -- tailscale serve's HTTPS mode has no hostname to use. See docs/REMOTE.md.",
    "serve_command_failed": "The 'tailscale serve' command itself failed. Run it by hand to see the real error.",
}


def _setup_tailscale(server, port: int):
    """Returns ``(tailscale_exe, dns_name)`` on success, ``(None, None)``
    on any failure -- always after printing a clear, specific reason, and
    always *before* handing anything to the user that would suggest
    Tailscale Serve is actually working when it isn't.
    """

    exe = tailscale.find_tailscale_exe()
    if not exe:
        print("Tailscale was not found (checked the Windows install path and PATH).", file=sys.stderr)
        print("Install Tailscale on Windows and make sure you're logged in, then try again.", file=sys.stderr)
        return None, None

    reachable = tailscale.windows_can_reach_backend(port)
    if reachable is None:
        print("Could not verify Windows -> WSL connectivity (curl.exe not found).", file=sys.stderr)
        print("Windows localhost에서 Tower Remote에 연결할 수 없습니다 -- 확인할 수 없어 진행하지 않습니다.", file=sys.stderr)
        return None, None
    if not reachable:
        print("Windows localhost에서 Tower Remote에 연결할 수 없습니다.", file=sys.stderr)
        print(f"(Windows curl.exe could not reach http://127.0.0.1:{port}/api/health)", file=sys.stderr)
        return None, None

    ok, detail = tailscale.start_serve(exe, port)
    if not ok:
        if detail.startswith("existing_mapping:"):
            existing = detail.split(":", 1)[1]
            print("A Tailscale Serve mapping already exists at :443, pointing somewhere else:", file=sys.stderr)
            print(f"  {existing}", file=sys.stderr)
            print("Tower will not overwrite it. Free it up yourself (tailscale serve --https=443 off)", file=sys.stderr)
            print("if that mapping is no longer needed, then try again.", file=sys.stderr)
        else:
            print(_TAILSCALE_ERROR_MESSAGES.get(detail, f"Tailscale Serve setup failed: {detail}"), file=sys.stderr)
        return None, None

    dns_name = detail
    status = tailscale.get_status(exe)
    self_ip = tailscale.self_tailscale_ip(status) if status else None
    if self_ip:
        server.allowed_hosts.append(self_ip)
    server.allowed_hosts.append(dns_name)

    return exe, dns_name


def _watch_stdin_for_regenerate(server) -> None:
    """Local-only pairing code regeneration: typing 'r' + Enter in the
    same terminal ``tower serve`` is running in. Deliberately never
    exposed over HTTP (see ``server/httpapi.py`` -- no endpoint calls
    ``PairingSession.regenerate()``), since a remote client being able to
    mint itself a fresh pairing code on demand would defeat the whole
    point of rate-limiting wrong guesses.
    """

    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return  # stdin closed (e.g. running under a non-interactive wrapper)
        if line.strip().lower() == "r":
            new_code = server.pairing.regenerate()
            print(f"\nNew pairing code: {new_code}  (valid 5 minutes, one-time use)")
            sys.stdout.flush()


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="tower", description="A local-first TUI control tower for monitoring coding agents across tmux panes.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--doctor", action="store_true", help="run environment diagnostics and exit")
    parser.add_argument(
        "--here",
        action="store_true",
        help="deprecated, no-op: running with no flags already runs in the current pane",
    )
    parser.add_argument(
        "--focus",
        action="store_true",
        help="jump to the currently active Tower pane; does not start a new Tower (for a Ctrl+b w-style binding)",
    )

    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser(
        "serve", help="start Tower Remote: a small local web UI + API for checking status from your phone"
    )
    serve_network_group = serve_parser.add_mutually_exclusive_group()
    serve_network_group.add_argument(
        "--lan",
        action="store_true",
        help="bind to all interfaces so devices on your LAN can connect (default: localhost only)",
    )
    serve_network_group.add_argument(
        "--tailscale",
        action="store_true",
        help="expose over your tailnet via a Windows-host Tailscale Serve HTTPS mapping (backend stays localhost-only)",
    )
    serve_parser.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT, help=f"port to listen on (default: {DEFAULT_SERVE_PORT})")

    args = parser.parse_args(argv)

    if args.command == "serve":
        run_serve(args.lan, args.port, use_tailscale=args.tailscale)
        return

    if args.version:
        print(f"tower {__version__}")
        return

    if args.doctor:
        raise SystemExit(run_doctor())

    if args.focus:
        focus_active_tower()
        return

    run_ui()


if __name__ == "__main__":
    cli()
