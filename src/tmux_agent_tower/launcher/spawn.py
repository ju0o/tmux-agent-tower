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


def _finish_new_pane(session: str, target: SpawnTarget, command: Optional[str], pane_id: str, agents_cfg: Dict[str, str], bindings) -> SpawnResult:
    """Title, optional start, and binding. The pane already exists and is new."""

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
        return SpawnResult(target, True, "시작됨")

    missing = agents_cfg.get(target.agent_label, target.agent_label)
    tmux_capture.run_tmux(
        ["select-pane", "-t", pane_id, "-T", _pane_title(target, False, missing)], capture=False
    )
    return SpawnResult(target, False, f"명령을 찾을 수 없습니다: {missing}")


# --- local ------------------------------------------------------------------


def spawn_local(
    session: str,
    targets: List[SpawnTarget],
    agents_cfg: Dict[str, str],
    layout: str = DEFAULT_LAYOUT,
    bindings=None,
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
    for target, _command in resolved[1:]:
        created_pane = tmux_capture.run_tmux(
            [
                "split-window", "-d", "-P", "-F", "#{pane_id}",
                "-t", window_id,
                "-c", target.project_path,
            ]
        )
        pane_fields = _id_fields(created_pane)
        pane_ids.append(pane_fields[0] if pane_fields else "")

    results = []
    for (target, command), pane_id in zip(resolved, pane_ids):
        if not pane_id:
            results.append(SpawnResult(target, False, "pane을 생성하지 못했습니다."))
            continue
        results.append(_finish_new_pane(session, target, command, pane_id, agents_cfg, bindings))

    # Layout only the window this call just created.
    tmux_capture.run_tmux(["select-layout", "-t", window_id, layout], capture=False)
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

    # One new window for this launch. A matching name on the remote host
    # is not reused. The id from tmux is the only target after this.
    first_path = shlex.quote(targets_with_commands[0][0].project_path)
    lines.append(
        f'if [ "$NEW_SESSION" = "1" ]; then '
        f'WIN_ID=$(tmux new-session -d -P -F "#{{window_id}}" -s "$SESS" -n "$WIN" -c {first_path}); '
        f'else '
        f'WIN_ID=$(tmux new-window -d -P -F "#{{window_id}}" -t "$SESS:" -n "$WIN" -c {first_path}); '
        f'fi'
    )
    lines.append('PANE_0=$(tmux display-message -p -t "$WIN_ID" "#{{pane_id}}")')

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
            lines.append(f'  echo "OK {index}"')
            lines.append("else")
            lines.append(f'  tmux select-pane -t "$NEW_PANE_{index}" -T {missing_title}')
            lines.append(f'  echo "MISSING {index}"')
            lines.append("fi")
        else:
            lines.append(f'tmux select-pane -t "$NEW_PANE_{index}" -T {missing_title}')
            lines.append(f'echo "MISSING {index}"')

    lines.append(f'tmux select-layout -t "$WIN_ID" {shlex.quote(layout)}')
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
