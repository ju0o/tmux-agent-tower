"""Process-table helpers used as auxiliary, best-effort signals.

Only used to (a) resolve a pane's full command line for more reliable
adapter matching than tmux's short ``#{pane_current_command}`` name, and
(b) as a cheap read-only ``ps`` snapshot. Nothing here writes to or signals
any process.
"""

from __future__ import annotations

import subprocess
from typing import Dict


def process_snapshot(timeout: float = 2.0) -> Dict[str, Dict[str, str]]:
    """``{pid: {"ppid": ..., "args": ...}}``. Read-only. Empty on failure."""

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return {}

    snapshot: Dict[str, Dict[str, str]] = {}
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, ppid = parts[0], parts[1]
        args = parts[2] if len(parts) == 3 else ""
        snapshot[pid] = {"ppid": ppid, "args": args}
    return snapshot


def cmdline_by_pid(timeout: float = 2.0) -> Dict[str, str]:
    """Return ``{pid_str: full_command_line}`` for every visible process."""

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return {}

    mapping: Dict[str, str] = {}

    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid, args = parts
        mapping[pid] = args

    return mapping


def ppid_by_pid(timeout: float = 2.0) -> Dict[str, str]:
    """``{pid: parent_pid}`` from the same read-only process table."""

    return {pid: rec["ppid"] for pid, rec in process_snapshot(timeout).items()}
