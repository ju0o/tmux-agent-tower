"""Who is running in a pane, and which project that cwd belongs to.

Agent names come from the process tree first. A pane title is a label
someone typed, so it loses to an executable and it loses to a shell that
is actually what is running. A parent terminal does not get to name the
agent its child is running.

Project names come from a git repo when there is one. A title that is
just an agent or tool name is not a project.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

_SHELLS = {"bash", "zsh", "fish", "sh", "ssh", "dash"}

# argv0 (or a path that *is* the program) → display name.
_BY_BASENAME = {
    "claude": "Claude",
    "codex": "Codex",
    "opencode": "OpenCode",
    "grok": "Grok",
    "cursor-agent": "Cursor",
}

# Strong, unique screen markers. Used only when the process table has
# nothing to say. A shell we already identified is never relabeled by these.
_UI_MARKERS = (
    ("Cursor", ("add a follow-up",)),
    ("Claude", ("? for shortcuts", "← for agents")),
    ("Codex", ("ask codex",)),
    ("OpenCode", ("esc interrupt",)),
)


def argv0(args: str) -> str:
    """Basename of the program, with a leading login dash stripped."""

    text = (args or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return ""
    return Path(parts[0]).name.lower().lstrip("-")


def agent_name_from_args(args: str) -> Optional[str]:
    """The agent this command line *is*, or None.

    Matching is on the executable, not on words inside a shell script.
    ``bash -c ...claude...`` stays a shell. Cursor's CLI is a binary
    named ``agent`` whose arguments point at ``cursor-agent/.../index.js``.
    """

    base = argv0(args)
    if not base or base in _SHELLS:
        return None
    if base in _BY_BASENAME:
        return _BY_BASENAME[base]
    low = (args or "").lower()
    # The program path itself contains the marker. A later argument that
    # merely mentions it does not count unless argv0 is the known wrapper.
    try:
        parts = shlex.split(args or "")
    except ValueError:
        parts = (args or "").split()
    program = (parts[0] if parts else "").lower()
    if "cursor-agent" in program:
        return "Cursor"
    if base in {"agent", "node", "nodejs"} and "cursor-agent" in low:
        return "Cursor"
    return None


def evidence_cmdline(
    pane_pid: str,
    command: str,
    cmdline_map: Dict[str, str],
    ppid_map: Dict[str, str],
) -> str:
    """Command line of the deepest agent under this pane.

    Shell children of an agent (tool calls) do not replace it. When no
    agent is in the tree, the pane's own command line is returned so a
    real shell can beat a misleading title.
    """

    children: Dict[str, list] = {}
    for pid, parent in ppid_map.items():
        children.setdefault(parent, []).append(pid)

    best_depth = -1
    best_args = ""
    stack = [(str(pane_pid), 0)]
    seen = set()
    while stack:
        pid, depth = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        args = cmdline_map.get(pid, "")
        if agent_name_from_args(args) and depth >= best_depth:
            best_depth = depth
            best_args = args
        for child in children.get(pid, []):
            stack.append((child, depth + 1))

    if best_args:
        return best_args
    own = cmdline_map.get(str(pane_pid), "")
    if own:
        return own
    return (command or "").strip()


def _title_agent(title: str) -> Optional[str]:
    low = (title or "").strip().lower()
    if not low:
        return None
    # Drop a leading spinner glyph Claude and others put on the title.
    while low and low[0] in "✳✻✽∗*· ":
        low = low[1:].lstrip()
    for base, name in _BY_BASENAME.items():
        if low == base or low.startswith(base + " "):
            return name
    if low == "cursor" or low.startswith("cursor "):
        return "Cursor"
    return None


def title_is_agent_label(title: Optional[str]) -> bool:
    """True when the title names an agent or tool, not a project."""

    return _title_agent(title or "") is not None


def _ui_agent(lines: Sequence[str]) -> Optional[str]:
    text = "\n".join(lines or ()).lower()
    if not text.strip():
        return None
    hits = []
    for name, markers in _UI_MARKERS:
        if any(marker in text for marker in markers):
            hits.append(name)
    if len(hits) == 1:
        return hits[0]
    return None


def _is_shell_command(command: str) -> bool:
    return (command or "").strip().lower().lstrip("-") in _SHELLS


def identify_agent(
    command: str,
    title: str,
    cmdline: str = "",
    lines: Sequence[str] = (),
) -> Tuple[str, str]:
    """``(agent name, source)``.

    Source is ``process``, ``ui``, ``title``, or ``shell``. An explicit
    user override is applied by the caller and reported as ``override``.
    """

    from_args = agent_name_from_args(cmdline)
    if from_args:
        return from_args, "process"

    from_command = _BY_BASENAME.get((command or "").strip().lower())
    if from_command:
        return from_command, "process"

    if _is_shell_command(command) or _is_shell_command(argv0(cmdline)):
        return "Shell", "process"

    from_ui = _ui_agent(lines)
    if from_ui:
        return from_ui, "ui"

    from_title = _title_agent(title)
    if from_title:
        return from_title, "title"

    return "Shell", "shell"


def prefer_override(auto_value: str, auto_source: str, override: Optional[str]) -> Tuple[str, str]:
    """A saved edit wins. Reset is the only way back to ``auto_source``."""

    if override:
        return override, "override"
    return auto_value, auto_source
