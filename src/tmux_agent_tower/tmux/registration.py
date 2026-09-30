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

from typing import Optional

from . import capture

PANE_OPTION = "@tmux_agent_tower_pane"
WINDOW_OPTION = "@tmux_agent_tower_window"


def register(session: str, pane_id: str, window_id: str = "") -> None:
    """Marks ``pane_id`` as the active Tower for ``session``.

    Last write wins by design (see module docstring in
    ``ui/tower.py::main`` / docs/ROADMAP.md): starting a second Tower in
    another pane is allowed, and it simply becomes the new focus target --
    older Tower processes are never killed.
    """

    capture.set_session_option(session, PANE_OPTION, pane_id)
    if window_id:
        capture.set_session_option(session, WINDOW_OPTION, window_id)


def resolve_active_pane(session: str) -> Optional[str]:
    """The currently-registered Tower pane for ``session``, or ``None``.

    A registration pointing at a pane that no longer exists (the Tower
    that registered it exited, tmux server restarted, ...) is stale and is
    cleared here rather than ever being handed to a caller.
    """

    pane_id = capture.get_session_option(session, PANE_OPTION)
    if not pane_id:
        return None

    if not capture.pane_exists(pane_id):
        capture.unset_session_option(session, PANE_OPTION)
        capture.unset_session_option(session, WINDOW_OPTION)
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
        capture.unset_session_option(session, PANE_OPTION)
        capture.unset_session_option(session, WINDOW_OPTION)


def focus_pane(pane_id: str) -> None:
    """Switches the attached client's view to ``pane_id`` -- selects both
    its window (so it's what a client sees) and the pane within it.
    """

    capture.run_tmux(["select-window", "-t", pane_id], capture=False)
    capture.run_tmux(["select-pane", "-t", pane_id], capture=False)
