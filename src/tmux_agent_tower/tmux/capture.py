"""Thin, defensive wrapper around the ``tmux`` binary.

Every call swallows exceptions and returns an empty result on failure
(missing binary, no server running, pane vanished mid-call, etc.) so a
transient tmux hiccup degrades a single row to UNKNOWN instead of crashing
the whole TUI.
"""

from __future__ import annotations

import subprocess
from typing import Sequence

DEFAULT_TIMEOUT = 3.0


def run_tmux(args: Sequence[str], capture: bool = True, timeout: float = DEFAULT_TIMEOUT) -> str:
    try:
        result = subprocess.run(
            ["tmux", *args],
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""

    if capture:
        return (result.stdout or "").rstrip("\n")
    return ""


def capture_pane(pane_id: str, lines: int = 30) -> list:
    output = run_tmux(["capture-pane", "-p", "-t", pane_id, "-S", f"-{lines}"])
    if not output:
        return []
    return output.split("\n")


def select_pane(session: str, window_index: str, pane_id: str) -> None:
    run_tmux(["select-window", "-t", f"{session}:{window_index}"], capture=False)
    run_tmux(["select-pane", "-t", pane_id], capture=False)


def list_windows(session: str) -> list:
    output = run_tmux(["list-windows", "-t", session, "-F", "#{window_name}"])
    return [line for line in output.split("\n") if line]


def new_control_window(session: str, window_name: str, command: str) -> None:
    run_tmux(["new-window", "-d", "-t", f"{session}:", "-n", window_name, command], capture=False)


def select_window(session: str, window_name: str) -> None:
    run_tmux(["select-window", "-t", f"{session}:{window_name}"], capture=False)


def current_session() -> str:
    return run_tmux(["display-message", "-p", "#S"]).strip()
