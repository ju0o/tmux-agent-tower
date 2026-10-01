"""Workspace Launcher: turn a confirmed plan into new tmux panes.

Hard rules enforced by this module (see PM approval / docs/ROADMAP.md):

* Only ever creates NEW windows/panes. Never sends keys into, closes, or
  otherwise touches a pane that existed before this call.
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
from .config import resolve_agent_command

DEFAULT_LAYOUT = "tiled"
LAYOUT_CHOICES = {
    "tiled": "tiled",
    "even-horizontal": "even-horizontal",
    "even-vertical": "even-vertical",
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


def _pane_title(target: SpawnTarget, ok: bool, missing_command: str = "") -> str:
    if ok:
        return f"{target.agent_label} - {target.project_name}"
    return f"{target.project_name} (명령 없음: {missing_command})"


def _resolve_all(targets: List[SpawnTarget], agents_cfg: Dict[str, str]) -> List[Tuple[SpawnTarget, Optional[str]]]:
    return [(t, resolve_agent_command(t.agent_label, agents_cfg)) for t in targets]


# --- local ------------------------------------------------------------------


def _list_pane_ids(session: str, window_name: str) -> List[str]:
    output = tmux_capture.run_tmux(["list-panes", "-t", f"{session}:{window_name}", "-F", "#{pane_id}"])
    return [line for line in output.split("\n") if line]


def spawn_local(
    session: str,
    window_name: str,
    targets: List[SpawnTarget],
    agents_cfg: Dict[str, str],
    layout: str = DEFAULT_LAYOUT,
    bindings=None,
) -> List[SpawnResult]:
    if not targets:
        return []

    resolved = _resolve_all(targets, agents_cfg)
    existing_windows = set(tmux_capture.run_tmux(["list-windows", "-t", session, "-F", "#{window_name}"]).split("\n"))
    window_exists = window_name in existing_windows

    results: List[SpawnResult] = []
    known_pane_ids = set(_list_pane_ids(session, window_name)) if window_exists else set()

    for index, (target, command) in enumerate(resolved):
        if index == 0 and not window_exists:
            tmux_capture.run_tmux(
                ["new-window", "-d", "-t", f"{session}:", "-n", window_name, "-c", target.project_path],
                capture=False,
            )
        else:
            tmux_capture.run_tmux(
                ["split-window", "-t", f"{session}:{window_name}", "-c", target.project_path],
                capture=False,
            )

        current_pane_ids = _list_pane_ids(session, window_name)
        new_ids = [p for p in current_pane_ids if p not in known_pane_ids]
        pane_id = new_ids[0] if new_ids else (current_pane_ids[-1] if current_pane_ids else "")
        known_pane_ids.add(pane_id)

        if not pane_id:
            results.append(SpawnResult(target, False, "pane을 생성하지 못했습니다."))
            continue

        if bindings is not None:
            pane_pid = tmux_capture.run_tmux(
                ["display-message", "-p", "-t", pane_id, "#{pane_pid}"]
            ).strip()
            bindings.record(
                pane_id,
                session,
                pane_pid,
                target.project_path,
                target.project_name,
                target.agent_label,
            )

        if command:
            tmux_capture.run_tmux(["send-keys", "-t", pane_id, command, "Enter"], capture=False)
            tmux_capture.run_tmux(["select-pane", "-t", pane_id, "-T", _pane_title(target, True)], capture=False)
            results.append(SpawnResult(target, True, "시작됨"))
        else:
            missing = agents_cfg.get(target.agent_label, target.agent_label)
            tmux_capture.run_tmux(
                ["select-pane", "-t", pane_id, "-T", _pane_title(target, False, missing)], capture=False
            )
            results.append(SpawnResult(target, False, f"명령을 찾을 수 없습니다: {missing}"))

    tmux_capture.run_tmux(["select-layout", "-t", f"{session}:{window_name}", layout], capture=False)
    return results


# --- remote (SSH, see docs/ARCHITECTURE.md for why this shape) -------------


def build_remote_script(window_name: str, targets_with_commands: List[Tuple[SpawnTarget, Optional[str]]], layout: str) -> str:
    """``targets_with_commands`` pairs each target with its *configured*
    command string (``agents_cfg.get(label)``), NOT a pre-checked one --
    existence is checked with ``command -v`` on the remote host itself,
    since a binary present on this machine may not exist on the far end
    (and vice versa).
    """

    lines = [
        "set -u",
        'SESS=$(tmux list-sessions -F "#{session_name}" 2>/dev/null | head -n1)',
        "NEW_SESSION=0",
        'if [ -z "$SESS" ]; then NEW_SESSION=1; SESS=tower; fi',
        f"WIN={shlex.quote(window_name)}",
    ]

    for index, (target, command) in enumerate(targets_with_commands):
        path_q = shlex.quote(target.project_path)

        if index == 0:
            # A brand-new session's own default first window is created
            # directly AS our target window (via `new-session -n`) instead
            # of `new-session` + a separate `new-window`, so no unused
            # leftover window is left behind. This path is only reachable
            # when we know for certain no session existed a moment ago, so
            # it can never disturb a pre-existing one.
            lines.append(
                f'if [ "$NEW_SESSION" = "1" ]; then tmux new-session -d -s "$SESS" -n "$WIN" -c {path_q}; '
                f'elif tmux list-windows -t "$SESS" -F "#{{window_name}}" 2>/dev/null | grep -Fxq "$WIN"; then '
                f'tmux split-window -t "${{SESS}}:${{WIN}}" -c {path_q}; '
                f'else tmux new-window -d -t "${{SESS}}:" -n "$WIN" -c {path_q}; fi'
            )
        else:
            lines.append(f'tmux split-window -t "${{SESS}}:${{WIN}}" -c {path_q}')

        lines.append(f'NEW_PANE_{index}=$(tmux list-panes -t "${{SESS}}:${{WIN}}" -F "#{{pane_id}}" | tail -n1)')

        ok_title = shlex.quote(_pane_title(target, True))
        missing_title = shlex.quote(_pane_title(target, False, command or "?"))

        if command:
            binary = shlex.quote(shlex.split(command)[0])
            lines.append(f"if command -v {binary} >/dev/null 2>&1; then")
            lines.append(f'  tmux send-keys -t "$NEW_PANE_{index}" {shlex.quote(command)} Enter')
            lines.append(f'  tmux select-pane -t "$NEW_PANE_{index}" -T {ok_title}')
            lines.append(f'  echo "OK {index}"')
            lines.append("else")
            lines.append(f'  tmux select-pane -t "$NEW_PANE_{index}" -T {missing_title}')
            lines.append(f'  echo "MISSING {index}"')
            lines.append("fi")
        else:
            lines.append(f'tmux select-pane -t "$NEW_PANE_{index}" -T {missing_title}')
            lines.append(f'echo "MISSING {index}"')

    lines.append(f'tmux select-layout -t "${{SESS}}:${{WIN}}" {shlex.quote(layout)}')
    return "\n".join(lines)


def spawn_remote(
    host_alias: str,
    window_name: str,
    targets: List[SpawnTarget],
    agents_cfg: Dict[str, str],
    layout: str = DEFAULT_LAYOUT,
    timeout: float = 8.0,
) -> List[SpawnResult]:
    if not targets:
        return []

    # Not pre-checked with local shutil.which() -- see build_remote_script.
    targets_with_commands = [(t, agents_cfg.get(t.agent_label)) for t in targets]
    script = build_remote_script(window_name, targets_with_commands, layout)

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

    if result.returncode != 0:
        return [SpawnResult(t, False, f"{host_alias}에 연결할 수 없습니다.") for t, _ in targets_with_commands]

    ok_indices = set()
    for line in (result.stdout or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "OK":
            try:
                ok_indices.add(int(parts[1]))
            except ValueError:
                pass

    results = []
    for index, (target, command) in enumerate(targets_with_commands):
        if index in ok_indices:
            results.append(SpawnResult(target, True, "시작됨"))
        else:
            missing = command or target.agent_label
            results.append(SpawnResult(target, False, f"명령을 찾을 수 없습니다: {missing}"))

    return results
