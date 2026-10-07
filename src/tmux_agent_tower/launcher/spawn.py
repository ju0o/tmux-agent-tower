"""Workspace Launcher: turn a confirmed plan into new tmux panes.

Hard rules enforced by this module (see PM approval / docs/ROADMAP.md):

* Only ever creates NEW windows/panes. Never sends keys into, closes,
  focuses, or retile a window or pane that existed before this call.
  A host label is not a window name, and a matching window name is not
  a reason to split that window. The new window's id is the only target.
* One target's missing agent command never aborts the rest -- every
  target gets its own pane either way (running the resolved agent command,
  or left at a plain shell with a "command not found" title) and its own
  ``SpawnResult``.
* Remote (SSH) spawn failures degrade to "can't reach host" results for
  every target in that batch; they never raise up into the UI.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..tmux import capture as tmux_capture
from ..tmux import structure as tmux_structure
from .config import resolve_agent_command

DEFAULT_LAYOUT = "tiled"
LAYOUT_CHOICES = {
    "tiled": "tiled",
    "even-horizontal": "even-horizontal",
    "even-vertical": "even-vertical",
    "main-horizontal": "main-horizontal",
    "main-vertical": "main-vertical",
}


@dataclass(frozen=True)
class SpawnTarget:
    project_path: str
    project_name: str
    agent_label: str


@dataclass(frozen=True)
class SpawnResult:
    target: SpawnTarget
    ok: bool
    detail: str
    pane_id: str = ""
    session: str = ""
    pane_pid: str = ""
    window_id: str = ""


def _pane_title(target: SpawnTarget, ok: bool, missing_command: str = "") -> str:
    if ok:
        return f"{target.agent_label} - {target.project_name}"
    return f"{target.project_name} (명령 없음: {missing_command})"


def _resolve_all(targets: List[SpawnTarget], agents_cfg: Dict[str, str]) -> List[Tuple[SpawnTarget, Optional[str]]]:
    return [(t, resolve_agent_command(t.agent_label, agents_cfg)) for t in targets]


def launch_window_name(targets: List[SpawnTarget]) -> str:
    """Display name only. Never a host label, and never used to find the window again.

    One project uses that project's name. Several projects share one
    workspace window. A colon would make a ``session:window`` target
    ambiguous, so it is removed even though later commands use the id.
    """

    if len(targets) <= 1:
        raw = (targets[0].project_name if targets else "") or "Tower"
    else:
        raw = "Tower Workspace"
    cleaned = "".join(ch if ch.isprintable() and ch != ":" else " " for ch in raw)
    cleaned = " ".join(cleaned.split())
    return (cleaned[:48] or "Tower")


def _id_fields(output: str) -> List[str]:
    return [part for part in (output or "").replace("\t", " ").split() if part]


def _tmux_ok(args: List[str]) -> bool:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3, check=False).returncode == 0
    except Exception:
        return False


def _finish_new_pane(
    session: str,
    target: SpawnTarget,
    command: Optional[str],
    pane_id: str,
    agents_cfg: Dict[str, str],
    bindings,
    overrides=None,
    transactional: bool = False,
) -> SpawnResult:
    """Title, optional start, and binding. The pane already exists and is new.

    A reused pane id must not keep the previous pane's override. The new
    process id does not match that record, so the record is dropped.
    """

    pane_pid = tmux_capture.run_tmux(
        ["display-message", "-p", "-t", pane_id, "#{pane_pid}"]
    ).strip()
    if transactional and not pane_pid.isdecimal():
        raise RuntimeError("new pane identity unavailable")
    if overrides is not None:
        overrides.drop_if_stale(pane_id, session, pane_pid)
    if bindings is not None:
        from ..detection.sshdest import destination_of_command

        ssh_target = destination_of_command(command or "")
        bindings.record(
            pane_id,
            session,
            pane_pid,
            target.project_path,
            target.project_name,
            target.agent_label,
            transport="ssh" if ssh_target else "",
            transport_target=ssh_target,
        )

    if command:
        send = ["send-keys", "-t", pane_id, command, "Enter"]
        title = ["select-pane", "-t", pane_id, "-T", _pane_title(target, True)]
        if transactional:
            if not _tmux_ok(send) or not _tmux_ok(title):
                raise RuntimeError("new agent command could not be sent")
        else:
            tmux_capture.run_tmux(send, capture=False)
            tmux_capture.run_tmux(title, capture=False)
        return SpawnResult(target, True, "시작됨", pane_id, session, pane_pid)

    missing = agents_cfg.get(target.agent_label, target.agent_label)
    tmux_capture.run_tmux(
        ["select-pane", "-t", pane_id, "-T", _pane_title(target, False, missing)], capture=False
    )
    return SpawnResult(target, False, f"명령을 찾을 수 없습니다: {missing}", pane_id, session, pane_pid)


# --- local ------------------------------------------------------------------


def spawn_local(
    session: str,
    targets: List[SpawnTarget],
    agents_cfg: Dict[str, str],
    layout: str = DEFAULT_LAYOUT,
    bindings=None,
    overrides=None,
    transactional: bool = False,
) -> List[SpawnResult]:
    """Open this launch in a brand-new window and nowhere else.

    The host label is not a window name. An existing window with the same
    display name is left alone, including its layout and which pane the
    client is looking at. ``new-window -d`` and ``split-window -d`` do not
    move the client. Every later command targets the returned window id.
    """

    if not targets:
        return []

    resolved = _resolve_all(targets, agents_cfg)
    window_name = launch_window_name([target for target, _command in resolved])
    created = tmux_capture.run_tmux(
        [
            "new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}",
            "-t", f"{session}:",
            "-n", window_name,
            "-c", resolved[0][0].project_path,
        ]
    )
    fields = _id_fields(created)
    if len(fields) < 2:
        return [SpawnResult(target, False, "pane을 생성하지 못했습니다.") for target, _command in resolved]

    window_id, pane_ids = fields[0], [fields[1]]
    if transactional:
        if not tmux_structure.valid_window_id(window_id) or not tmux_structure.valid_pane_id(pane_ids[0]):
            return [SpawnResult(target, False, "새 작업 위치를 확인할 수 없습니다.") for target, _ in resolved]
    try:
        for target, _command in resolved[1:]:
            created_pane = tmux_capture.run_tmux(
                [
                    "split-window", "-d", "-P", "-F", "#{pane_id}",
                    "-t", window_id,
                    "-c", target.project_path,
                ]
            )
            pane_fields = _id_fields(created_pane)
            if not pane_fields or (transactional and not tmux_structure.valid_pane_id(pane_fields[0])):
                raise RuntimeError("pane create failed")
            pane_ids.append(pane_fields[0])
    except Exception:
        if transactional:
            tmux_structure.kill_window(session, window_id)
            return [SpawnResult(target, False, "작업 구성을 만들지 못해 새 작업을 정리했습니다.") for target, _ in resolved]
        pane_ids.append("")

    if transactional and len(pane_ids) != len(resolved):
        tmux_structure.kill_window(session, window_id)
        return [SpawnResult(target, False, "작업 구성을 만들지 못해 새 작업을 정리했습니다.") for target, _ in resolved]

    results = []
    try:
        for (target, command), pane_id in zip(resolved, pane_ids):
            if not pane_id:
                results.append(SpawnResult(target, False, "pane을 생성하지 못했습니다."))
                continue
            result = _finish_new_pane(
                session, target, command, pane_id, agents_cfg, bindings, overrides,
                transactional=transactional,
            )
            results.append(SpawnResult(
                result.target, result.ok, result.detail, result.pane_id,
                result.session, result.pane_pid, window_id,
            ))

        # Layout only the window this call just created.
        if transactional:
            if not tmux_structure.apply_layout(session, window_id, layout).ok:
                raise RuntimeError("layout failed")
        else:
            tmux_capture.run_tmux(["select-layout", "-t", window_id, layout], capture=False)
        if transactional and any(not result.ok for result in results):
            raise RuntimeError("agent start failed")
    except Exception:
        if transactional:
            tmux_structure.kill_window(session, window_id)
            return [SpawnResult(target, False, "작업을 안전하게 시작하지 못해 새 작업을 정리했습니다.") for target, _ in resolved]
        raise

    return results


def spawn_into_window(
    session: str,
    window_id: str,
    target: SpawnTarget,
    agents_cfg: Dict[str, str],
    bindings=None,
    overrides=None,
    direction: str = "auto",
) -> SpawnResult:
    """Split one new pane into ``window_id`` only. Other windows stay as they are.

    Does not apply a layout. The split itself is the only change to that window.
    """

    from ..tmux.structure import create_pane

    created = create_pane(window_id, direction=direction, cwd=target.project_path)
    if not created.ok or not created.pane_id:
        return SpawnResult(target, False, "pane을 생성하지 못했습니다.")
    command = resolve_agent_command(target.agent_label, agents_cfg)
    return _finish_new_pane(session, target, command, created.pane_id, agents_cfg, bindings, overrides)


# --- remote (SSH, see docs/ARCHITECTURE.md for why this shape) -------------


def build_remote_script(
    window_name: str,
    targets_with_commands: List[Tuple[SpawnTarget, Optional[str]]],
    layout: str,
    destination: Optional[Tuple[str, str]] = None,
    transactional: bool = False,
) -> str:
    """``targets_with_commands`` pairs each target with its *configured*
    command string (``agents_cfg.get(label)``), NOT a pre-checked one --
    existence is checked with ``command -v`` on the remote host itself,
    since a binary present on this machine may not exist on the far end
    (and vice versa).
    """

    if layout not in LAYOUT_CHOICES.values():
        raise ValueError("unsupported layout")

    lines = ["set -eu" if destination or transactional else "set -u"]
    if transactional:
        if destination:
            raise ValueError("transactional launch requires a new window")
        lines.extend([
            'CREATED_RESOURCES=0; WIN_ID=""',
            'cleanup() { status=$?; if [ "$status" -ne 0 ] && [ "$CREATED_RESOURCES" = "1" ]; then '
            'tmux kill-window -t "$WIN_ID" 2>/dev/null || true; fi; exit "$status"; }',
            "trap cleanup EXIT",
        ])
        for index, (_target, command) in enumerate(targets_with_commands):
            if not command:
                raise ValueError(f"missing remote command for target {index}")
            try:
                binary = shlex.split(command)[0]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"invalid remote command for target {index}") from exc
            lines.extend([
                f"if ! command -v {shlex.quote(binary)} >/dev/null 2>&1; then",
                f"  printf 'ERROR\\tunavailable\\t{index}\\n'",
                "  exit 42",
                "fi",
            ])
    if destination:
        remote_session, window_id = destination
        from ..tmux.structure import valid_window_id

        if not remote_session or not valid_window_id(window_id):
            raise ValueError("bad remote destination")
        lines.extend([f"SESS={shlex.quote(remote_session)}", f"WIN_ID={shlex.quote(window_id)}"])
    else:
        lines.extend([
            'SESS=$(tmux list-sessions -F "#{session_name}" 2>/dev/null | head -n1)',
            "NEW_SESSION=0",
            'if [ -z "$SESS" ]; then NEW_SESSION=1; SESS=tower; fi',
            f"WIN={shlex.quote(window_name)}",
        ])

    # One new window for this launch. A matching name on the remote host
    # is not reused. The id from tmux is the only target after this.
    first_path = shlex.quote(targets_with_commands[0][0].project_path)
    if destination:
        if len(targets_with_commands) != 1:
            raise ValueError("remote destination accepts one task")
        lines.append(
            f'PANE_0=$(tmux split-window -d -P -F "#{{pane_id}}" -t "$WIN_ID" -c {first_path})'
        )
    else:
        lines.append(
            f'if [ "$NEW_SESSION" = "1" ]; then '
            f'CREATED=$(tmux new-session -d -P -F "#{{window_id}} #{{pane_id}}" -s "$SESS" -n "$WIN" -c {first_path}) || exit 44; '
            f'else '
            f'CREATED=$(tmux new-window -d -P -F "#{{window_id}} #{{pane_id}}" -t "$SESS:" -n "$WIN" -c {first_path}) || exit 44; '
            f'fi; WIN_ID=${{CREATED%% *}}; PANE_0=${{CREATED#* }}'
        )
        if transactional:
            lines.append('CREATED_RESOURCES=1')

    for index, (target, command) in enumerate(targets_with_commands):
        path_q = shlex.quote(target.project_path)

        if index == 0:
            lines.append(f'NEW_PANE_{index}="$PANE_0"')
        else:
            lines.append(
                f'NEW_PANE_{index}=$(tmux split-window -d -P -F "#{{pane_id}}" -t "$WIN_ID" -c {path_q})'
            )

        ok_title = shlex.quote(_pane_title(target, True))
        missing_title = shlex.quote(_pane_title(target, False, command or "?"))

        if command:
            binary = shlex.quote(shlex.split(command)[0])
            lines.append(f"if command -v {binary} >/dev/null 2>&1; then")
            lines.append(f'  tmux send-keys -t "$NEW_PANE_{index}" {shlex.quote(command)} Enter')
            lines.append(f'  tmux select-pane -t "$NEW_PANE_{index}" -T {ok_title}')
            if transactional:
                lines.append(
                    f'  printf \'OK\\t{index}\\t%s\\t%s\\t%s\\t%s\\n\' "$NEW_PANE_{index}" "$SESS" '
                    f'"$(tmux display-message -p -t "$NEW_PANE_{index}" "#{{pane_pid}}")" "$WIN_ID"'
                )
            else:
                lines.append(
                    f'  printf \'OK\\t{index}\\t%s\\t%s\\t%s\\n\' "$NEW_PANE_{index}" "$SESS" '
                    f'"$(tmux display-message -p -t "$NEW_PANE_{index}" "#{{pane_pid}}")"'
                )
            lines.append("else")
            lines.append(f'  tmux select-pane -t "$NEW_PANE_{index}" -T {missing_title}')
            lines.append(
                f'  printf \'MISSING\\t{index}\\t%s\\t%s\\t%s\\n\' "$NEW_PANE_{index}" "$SESS" '
                f'"$(tmux display-message -p -t "$NEW_PANE_{index}" "#{{pane_pid}}")"'
            )
            if transactional:
                lines.append("  exit 43")
            lines.append("fi")
        else:
            lines.append(f'tmux select-pane -t "$NEW_PANE_{index}" -T {missing_title}')
            lines.append(
                f'printf \'MISSING\\t{index}\\t%s\\t%s\\t%s\\n\' "$NEW_PANE_{index}" "$SESS" '
                f'"$(tmux display-message -p -t "$NEW_PANE_{index}" "#{{pane_pid}}")"'
            )

    if not destination:
        lines.append(f'tmux select-layout -t "$WIN_ID" {shlex.quote(layout)}')
    if transactional:
        lines.append("trap - EXIT")
    return "\n".join(lines)


def spawn_remote(
    host_alias: str,
    window_name: str,
    targets: List[SpawnTarget],
    agents_cfg: Dict[str, str],
    layout: str = DEFAULT_LAYOUT,
    timeout: float = 8.0,
    destination: Optional[Tuple[str, str]] = None,
    transactional: bool = False,
) -> List[SpawnResult]:
    if not targets:
        return []

    # Not pre-checked with local shutil.which() -- see build_remote_script.
    targets_with_commands = [(t, agents_cfg.get(t.agent_label)) for t in targets]
    try:
        script = build_remote_script(window_name, targets_with_commands, layout, destination, transactional)
    except ValueError as exc:
        detail = "지원하지 않는 작업 배치입니다." if "layout" in str(exc) else "작업 묶음을 찾지 못했습니다."
        return [SpawnResult(target, False, detail) for target in targets]

    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}", host_alias, script],
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
            check=False,
        )
    except Exception:
        return [SpawnResult(t, False, f"{host_alias}에 연결할 수 없습니다.") for t, _ in targets_with_commands]

    outcomes = {}
    unavailable = None
    from ..tmux.structure import valid_pane_id

    for line in (result.stdout or "").splitlines():
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) == 3 and parts[0] == "ERROR" and parts[1] == "unavailable":
            try:
                unavailable = int(parts[2])
            except ValueError:
                unavailable = None
            continue
        if len(parts) in (5, 6) and parts[0] in ("OK", "MISSING"):
            try:
                index = int(parts[1])
            except ValueError:
                continue
            pane_id, remote_session, pane_pid = parts[2:5]
            window_id = parts[5] if len(parts) == 6 else ""
            if (
                index < 0
                or index >= len(targets_with_commands)
                or not valid_pane_id(pane_id)
                or not remote_session
                or any(ord(ch) < 32 for ch in remote_session)
                or (pane_pid and not pane_pid.isdecimal())
                or (window_id and not tmux_structure.valid_window_id(window_id))
            ):
                continue
            outcomes[index] = (parts[0] == "OK", pane_id, remote_session, pane_pid, window_id)

    if transactional and result.returncode != 0:
        detail = (
            f"{targets_with_commands[unavailable][0].agent_label}를 원격 컴퓨터에서 사용할 수 없습니다."
            if unavailable is not None
            else "원격 작업 구성을 만들지 못해 이번 실행 항목을 정리했습니다."
        )
        return [SpawnResult(target, False, detail) for target, _ in targets_with_commands]

    results = []
    for index, (target, command) in enumerate(targets_with_commands):
        outcome = outcomes.get(index)
        if outcome is None:
            detail = (
                f"{targets_with_commands[unavailable][0].agent_label}를 원격 컴퓨터에서 사용할 수 없습니다."
                if unavailable is not None
                else f"{host_alias}에 연결할 수 없습니다."
                if result.returncode != 0
                else "시작 결과를 확인할 수 없습니다."
            )
            results.append(SpawnResult(target, False, detail))
            continue
        ok, pane_id, remote_session, pane_pid, window_id = outcome
        if ok:
            results.append(SpawnResult(target, True, "시작됨", pane_id, remote_session, pane_pid, window_id))
        else:
            missing = command or target.agent_label
            results.append(
                SpawnResult(target, False, f"명령을 찾을 수 없습니다: {missing}", pane_id, remote_session, pane_pid)
            )

    return results
