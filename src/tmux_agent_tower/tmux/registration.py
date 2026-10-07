"""Where is "the" running Tower, right now, in this session?

v0.1.0 answered that by window *name* ("CONTROL") -- fragile, because a
window's name is just text a user (or another tool) can freely reuse or
rename, and has no actual connection to what's running in it. A real bug
report confirmed this: a window that used to host Tower got reused for an
unrelated Claude session, kept the name "CONTROL", and `tower`/`Ctrl+b w`
kept jumping there instead of to the pane actually running Tower.

The fix: Tower registers its own pane_id -- the one true identity a pane
has -- in a session-scoped tmux user option when it starts, and every
lookup verifies that pane still exists before trusting it. Nothing here
ever matches on a window name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import capture

PANE_OPTION = "@tmux_agent_tower_pane"
WINDOW_OPTION = "@tmux_agent_tower_window"
PID_OPTION = "@tmux_agent_tower_pid"
SOURCE_OPTION = "@tmux_agent_tower_source"
_IDENTITY_OPTIONS = (PANE_OPTION, WINDOW_OPTION, PID_OPTION, SOURCE_OPTION)


def pid_alive(pid: str) -> bool:
    """True when ``pid`` is a live process. A reused pane id is not enough."""

    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def _ppid(pid: str) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    end = stat.rfind(")")
    if end < 0:
        return ""
    fields = stat[end + 2 :].split()
    return fields[1] if len(fields) > 1 else ""


def process_belongs_to_pane(pid: str, shell_pid: str) -> bool:
    """True when ``pid`` is the pane shell or a child of it.

    Eight steps is enough for a shell that exec'd Tower, or a shell whose
    child is Tower. A pid that merely happens to be alive is not this pane.
    """

    current = str(pid or "")
    shell = str(shell_pid or "")
    if not current or not shell:
        return False
    for _ in range(8):
        if current == shell:
            return True
        if current in {"", "0", "1"}:
            return False
        current = _ppid(current)
    return False


def _pane_shell_pid(pane_id: str) -> str:
    raw = capture.run_tmux(["display-message", "-p", "-t", pane_id, "#{pane_pid}"])
    return (raw or "").strip().splitlines()[0] if raw else ""


def _clear(session: str) -> None:
    for name in _IDENTITY_OPTIONS:
        capture.unset_session_option(session, name)


def register(session: str, pane_id: str, window_id: str = "", pid: str = "", source: str = "") -> None:
    """Marks ``pane_id`` as the active Tower for ``session``.

    Last write wins by design (see module docstring in
    ``ui/tower.py::main`` / docs/ROADMAP.md): starting a second Tower in
    another pane is allowed, and it simply becomes the new focus target --
    older Tower processes are never killed.

    ``pid`` and ``source`` identify the process that drew this Tower.
    A later lookup rejects the pane when that process is gone or is not
    in the pane. An empty ``pid`` is the older registration, which only
    checks that the pane is still alive.
    """

    capture.set_session_option(session, PANE_OPTION, pane_id)
    if window_id:
        capture.set_session_option(session, WINDOW_OPTION, window_id)
    else:
        capture.unset_session_option(session, WINDOW_OPTION)
    if pid:
        capture.set_session_option(session, PID_OPTION, str(pid))
    else:
        capture.unset_session_option(session, PID_OPTION)
    if source:
        capture.set_session_option(session, SOURCE_OPTION, source)
    else:
        capture.unset_session_option(session, SOURCE_OPTION)


def resolve_active_pane(session: str) -> Optional[str]:
    """The currently-registered Tower pane for ``session``, or ``None``.

    A registration pointing at a pane that no longer exists (the Tower
    that registered it exited, tmux server restarted, ...) is stale and is
    cleared here rather than ever being handed to a caller.
    """

    pane_id = capture.get_session_option(session, PANE_OPTION)
    if not pane_id:
        return None

    # Alive, and in this session. A pane id from another session, or a
    # dead pane that has not been destroyed yet, is stale. Never search
    # for Tower by window name.
    if not capture.pane_is_live_in_session(pane_id, session):
        _clear(session)
        return None

    pid = capture.get_session_option(session, PID_OPTION)
    if not pid:
        return pane_id
    if not pid_alive(pid) or not process_belongs_to_pane(pid, _pane_shell_pid(pane_id)):
        _clear(session)
        return None

    return pane_id


def unregister_if_self(session: str, pane_id: str) -> None:
    """Clears the registration only if it still points at ``pane_id``.

    A Tower exiting must never erase a *different*, newer Tower's
    registration (see the "multiple Tower" semantics in the module
    docstring) -- e.g. Tower A (%14) is still running when Tower B (%19)
    starts and becomes active; A exiting later must leave B's registration
    (%19) alone.
    """

    current = capture.get_session_option(session, PANE_OPTION)
    if current == pane_id:
        _clear(session)


def focus_pane(pane_id: str) -> None:
    """Switches the attached client's view to ``pane_id`` -- selects both
    its window (so it's what a client sees) and the pane within it.
    """

    capture.run_tmux(["select-window", "-t", pane_id], capture=False)
    capture.run_tmux(["select-pane", "-t", pane_id], capture=False)
