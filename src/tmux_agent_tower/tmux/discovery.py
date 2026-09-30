"""Enumerate local tmux panes into plain-dict "observations".

This module only *reads* tmux state (list-panes, capture-pane) and the
process table. It never renames, kills, or sends input to anything.
"""

from __future__ import annotations

from typing import Dict, List

from . import capture
from ..detection import process as process_detection
from ..detection.project import discover_project

FIELD_SEP = "\x1f"

_PANE_FORMAT = FIELD_SEP.join(
    [
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_index}",
        "#{pane_id}",
        "#{pane_title}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{pane_pid}",
        "#{pane_dead}",
    ]
)

_EXPECTED_FIELDS = _PANE_FORMAT.count(FIELD_SEP) + 1


def list_panes(session: str, exclude_pane_id: str = "", capture_lines: int = 30) -> List[Dict]:
    """Return one dict per pane, excluding Tower's own pane (by pane_id).

    Deliberately NOT a window-name match: a window's name is just text
    anyone can reuse or rename, with no real connection to what's running
    in it (a real bug: a window that used to host Tower got reused for an
    unrelated session, kept its old name, and got wrongly excluded/matched
    by name). ``exclude_pane_id`` is the one thing that's actually stable.

    Malformed lines (wrong field count -- e.g. a tmux version with a
    slightly different format quirk) are skipped rather than raising, so one
    bad line can't take down the whole refresh.
    """

    output = capture.run_tmux(["list-panes", "-s", "-t", session, "-F", _PANE_FORMAT])

    if not output:
        return []

    cmdline_map = process_detection.cmdline_by_pid()

    rows: List[Dict] = []

    for line in output.split("\n"):
        if not line:
            continue

        parts = line.split(FIELD_SEP)
        if len(parts) != _EXPECTED_FIELDS:
            continue

        (
            session_name,
            window_index,
            window_name,
            pane_index,
            pane_id,
            title,
            command,
            current_path,
            pane_pid,
            dead,
        ) = parts

        if exclude_pane_id and pane_id == exclude_pane_id:
            continue

        lines = capture.capture_pane(pane_id, lines=capture_lines) if dead != "1" else []

        rows.append(
            {
                "session": session_name,
                "window_index": window_index,
                "window_name": window_name,
                "pane_index": pane_index,
                "pane_id": pane_id,
                "title": title or "(unnamed)",
                "command": command,
                "cmdline": cmdline_map.get(pane_pid, ""),
                "path": current_path,
                "auto_project": discover_project(current_path),
                "dead": dead == "1",
                "lines": lines,
            }
        )

    return rows
