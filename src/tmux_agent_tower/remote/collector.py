"""Minimal multi-host prototype (P3).

Deliberately the smallest thing that could work: no daemon, no new service,
no credentials beyond the user's existing ``ssh <alias>`` setup. The remote host
runs a *read-only* one-shot ``tmux list-panes`` / ``tmux capture-pane``
query over the user's existing SSH connection and parses the result.
Nothing is installed on the remote host, and nothing this module does can
kill, restart, or send input to a remote pane or process.

Failure handling is graceful by design: any SSH error, timeout, or
malformed output degrades that single host to ``status: "OFFLINE"`` (or
``"UNKNOWN"`` for malformed data) with an empty pane list -- it never
raises, and it never blocks the local TUI beyond ``timeout`` seconds.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Dict, List

HOST_STATUS_ONLINE = "ONLINE"
HOST_STATUS_OFFLINE = "OFFLINE"
HOST_STATUS_UNKNOWN = "UNKNOWN"

DEFAULT_TIMEOUT = 4.0
_PANE_ID_RE = re.compile(r"^%\d+$")
_PID_RE = re.compile(r"^\d+$")
_SESSION_ID_RE = re.compile(r"^\$\d+$")

# A single remote invocation: list every pane's identifying fields, then for
# each pane id capture its tail. Kept as one SSH round trip for latency.
_REMOTE_SNAPSHOT_SCRIPT = r"""
set -u
if ! command -v tmux >/dev/null 2>&1; then
    echo "__TOWER_NO_TMUX__"
    exit 0
fi
if ! tmux list-sessions >/dev/null 2>&1; then
    echo "__TOWER_NO_SERVER__"
    exit 0
fi
tmux list-panes -a -F '#{session_name}\x1f#{session_id}\x1f#{window_id}\x1f#{window_index}\x1f#{window_name}\x1f#{window_created}\x1f#{pane_index}\x1f#{pane_id}\x1f#{pane_title}\x1f#{pane_current_command}\x1f#{pane_current_path}\x1f#{pane_pid}\x1f#{pane_dead}'
"""


def _run_ssh(host_alias: str, script: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(timeout))}",
            host_alias,
            script,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout + 1.0,
        check=False,
    )


def fetch_remote(host_alias: str, display_name: str = None, timeout: float = DEFAULT_TIMEOUT) -> Dict:
    """Fetch a best-effort snapshot of ``host_alias``'s tmux panes.

    Read-only: only ``tmux list-panes``/``tmux capture-pane`` are ever run
    on the remote side, over the user's own pre-existing SSH alias.
    """

    display_name = display_name or host_alias

    try:
        result = _run_ssh(host_alias, _REMOTE_SNAPSHOT_SCRIPT, timeout)
    except subprocess.TimeoutExpired:
        return {"host": display_name, "status": HOST_STATUS_OFFLINE, "panes": []}
    except Exception:
        return {"host": display_name, "status": HOST_STATUS_OFFLINE, "panes": []}

    if result.returncode != 0:
        return {"host": display_name, "status": HOST_STATUS_OFFLINE, "panes": []}

    return parse_remote_snapshot(display_name, result.stdout)


def fetch_remote_history(
    host_alias: str,
    pane_id: str,
    pane_pid: str,
    lines: int = 800,
    timeout: float = DEFAULT_TIMEOUT,
    session_id: str = "",
    join_wrapped: bool = False,
) -> List[str] | None:
    """Read bounded history for one registered remote tmux pane.

    Pane PID must still match the overview snapshot, so a reused pane ID
    cannot redirect Result extraction to a different process.
    """

    if (
        not host_alias
        or host_alias.startswith("-")
        or any(ch.isspace() or not ch.isprintable() for ch in host_alias)
        or not _PANE_ID_RE.fullmatch(pane_id or "")
        or not _PID_RE.fullmatch(str(pane_pid or ""))
        or not 1 <= lines <= 1200
        or (session_id and not _SESSION_ID_RE.fullmatch(session_id))
    ):
        return None
    session_check = (
        f"test \"$(tmux display-message -p -t {pane_id} '#{{session_id}}')\" = {shlex.quote(session_id)} || exit 4; "
        if session_id else ""
    )
    script = (
        f"test \"$(tmux display-message -p -t {pane_id} '#{{pane_pid}}')\" = {pane_pid} || exit 4; "
        f"test \"$(tmux display-message -p -t {pane_id} '#{{pane_dead}}')\" = 0 || exit 4; "
        f"{session_check}"
        f"tmux capture-pane -p{' -J' if join_wrapped else ''} -t {pane_id} -S -{lines}"
    )
    try:
        result = _run_ssh(host_alias, script, timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").rstrip("\n").split("\n") if result.stdout else []


def parse_remote_snapshot(display_name: str, raw_stdout: str) -> Dict:
    text = (raw_stdout or "").strip()

    if not text or text == "__TOWER_NO_TMUX__":
        return {"host": display_name, "status": HOST_STATUS_UNKNOWN, "panes": []}

    if text == "__TOWER_NO_SERVER__":
        # Reachable, tmux installed, but nothing running -- a legitimate,
        # non-error "online with zero panes" state.
        return {"host": display_name, "status": HOST_STATUS_ONLINE, "panes": []}

    panes: List[Dict] = []

    for line in text.split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 12:  # older Tower peer without the window creation marker
            parts.insert(5, "")
        if len(parts) != 13:
            # Malformed line: skip it rather than failing the whole host.
            continue

        (
            session,
            session_id,
            window_id,
            window_index,
            window_name,
            window_created,
            pane_index,
            pane_id,
            title,
            command,
            path,
            pane_pid,
            dead,
        ) = parts

        panes.append(
            {
                "session": session,
                "session_id": session_id,
                "window_id": window_id,
                "window_index": window_index,
                "window_name": window_name,
                "window_created": window_created,
                "pane_index": pane_index,
                "pane_id": pane_id,
                "pane_pid": pane_pid,
                "title": title or "(unnamed)",
                "command": command,
                "cmdline": "",
                "path": path,
                "dead": dead == "1",
                # No content capture in the v0.1.0 prototype snapshot (kept
                # to a single SSH round trip); status is therefore
                # necessarily coarser than local panes -- see README limits.
                "lines": [],
            }
        )

    if not panes:
        return {"host": display_name, "status": HOST_STATUS_ONLINE, "panes": []}

    return {"host": display_name, "status": HOST_STATUS_ONLINE, "panes": panes}
