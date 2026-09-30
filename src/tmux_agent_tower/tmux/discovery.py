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


def list_panes(session: str, exclude_window: str, capture_lines: int = 30) -> List[Dict]:
    """Return one dict per pane, excluding the control window itself.

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

        if window_name == exclude_window:
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
