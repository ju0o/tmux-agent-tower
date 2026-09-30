"""Read-write tmux actions the UI can trigger (pane navigation only).

Nothing in this module sends keystrokes into a pane, kills a pane/process,
or restarts anything. It only moves the user's own cursor between
windows/panes that already exist -- the same as the user pressing
``Ctrl+b`` themselves.
"""

from __future__ import annotations

from . import capture


def open_pane(session: str, window_index: str, pane_id: str) -> None:
    capture.select_pane(session, window_index, pane_id)


def ensure_control_window(session: str, window_name: str, launch_command: str) -> None:
    if window_name not in capture.list_windows(session):
        capture.new_control_window(session, window_name, launch_command)


def return_to_control(session: str, window_name: str) -> None:
    capture.select_window(session, window_name)
