"""Grok CLI adapter.

Evidence: Grok CLI draws a boxed input prompt at the bottom
(``╭─...─╮`` / ``❯`` / ``╰─ Grok ... ─╯``). While a background task/loop is
still active it shows a status line such as ``1 loop still running`` above
the box; when idle the same area reads ``send a message`` with nothing
running.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from .base import Activity, AgentAdapter, AdapterResult, PaneContext, ResultCandidate, result_fingerprint

_RUNNING_RE = re.compile(r"loop[s]?\s+(is\s+|are\s+)?still running", re.IGNORECASE)
_IDLE_HINT_RE = re.compile(r"send a message", re.IGNORECASE)
# e.g. "⸬ Task Locate JuTell config and b… (4) 38s [↗][✗]" -- the task
# description between "Task " and its own "(N) <elapsed>" metadata.
_TASK_RE = re.compile(r"Task\s+(.+?)\s*\(\d+\)\s*\d+[smh]\b")
_SESSION_ID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_MESSAGE_HEADING_RE = re.compile(r"(?m)^## (User|Assistant)\s*$")
_MAX_EXPORT_BYTES = 2 * 1024 * 1024
_EXPORT_CACHE: Dict[tuple, Optional[ResultCandidate]] = {}


def _process_descendants(root: str):
    """Read a bounded Linux process subtree rooted at tmux's pane pid."""

    if not str(root).isdigit():
        return []
    stack = [(str(root), 0)]
    seen = set()
    grok_pids = []
    while stack and len(seen) < 256:
        pid, depth = stack.pop()
        if pid in seen or depth > 32:
            continue
        seen.add(pid)
        proc = Path("/proc") / pid
        try:
            if (proc / "comm").read_text(encoding="utf-8").strip().lower() == "grok":
                grok_pids.append(pid)
            children = (proc / "task" / pid / "children").read_text(encoding="ascii").split()
        except OSError:
            continue
        stack.extend((child, depth + 1) for child in children if child.isdigit())
    return grok_pids


def _session_process(pane_pid: str):
    """Return the unique Grok session opened by this pane, else fail closed."""

    matches = []
    for pid in _process_descendants(pane_pid):
        proc = Path("/proc") / pid
        try:
            fds = list((proc / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                path = os.readlink(fd)
            except OSError:
                continue
            if not path.endswith("/events.jsonl"):
                continue
            session_id = Path(path).parent.name
            if _SESSION_ID_RE.fullmatch(session_id):
                try:
                    executable = os.readlink(proc / "exe").removesuffix(" (deleted)")
                    info = os.stat(path)
                except OSError:
                    continue
                matches.append((pid, session_id, path, executable, info.st_mtime_ns, info.st_size))
    unique = {(item[0], item[1], item[2], item[3], item[4], item[5]) for item in matches}
    return next(iter(unique)) if len(unique) == 1 else None


def _turn_state(events_path: str, session_id: str) -> Tuple[Optional[int], bool]:
    """Grok's own event log is the turn boundary, including external turns."""

    latest_turn = None
    latest_event = ""
    try:
        with open(events_path, encoding="utf-8", errors="replace") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                kind = event.get("type")
                if kind == "turn_started" and event.get("session_id") == session_id:
                    number = event.get("turn_number")
                    if isinstance(number, int) and number >= 0:
                        latest_turn, latest_event = number, kind
                elif kind == "turn_ended" and latest_turn is not None:
                    latest_event = kind
    except OSError:
        return None, False
    return latest_turn, latest_event == "turn_ended"


def _complete_answer(transcript: str, turn_number: int) -> Optional[str]:
    """Accept only a full, alternating Grok export through the latest turn."""

    headings = list(_MESSAGE_HEADING_RE.finditer(transcript))
    roles = [match.group(1) for match in headings]
    expected = [role for _ in range(turn_number + 1) for role in ("User", "Assistant")]
    if roles != expected or not roles or roles[-1] != "Assistant":
        return None
    match = headings[-1]
    end = len(transcript)
    answer = transcript[match.end():end].strip()
    return answer or None


def _export_result(session, turn_number: int) -> Optional[ResultCandidate]:
    pid, session_id, _events_path, executable, mtime_ns, size = session
    cache_key = (pid, session_id, turn_number, mtime_ns, size)
    if cache_key in _EXPORT_CACHE:
        return _EXPORT_CACHE[cache_key]
    result = None
    try:
        completed = subprocess.run(
            [executable, "export", session_id],
            capture_output=True,
            timeout=5,
            check=False,
        )
        raw = completed.stdout or b""
        if completed.returncode == 0 and raw and len(raw) <= _MAX_EXPORT_BYTES:
            transcript = raw.decode("utf-8", "strict")
            answer = _complete_answer(transcript, turn_number)
            if answer:
                result = ResultCandidate(
                    text=answer,
                    fingerprint=result_fingerprint(answer),
                    confidence="high",
                    complete=True,
                    source="grok_export",
                    turn_identity=f"{session_id}:{turn_number}",
                )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    if len(_EXPORT_CACHE) >= 32:
        _EXPORT_CACHE.pop(next(iter(_EXPORT_CACHE)))
    _EXPORT_CACHE[cache_key] = result
    return result


class GrokAdapter(AgentAdapter):
    name = "Grok"
    command_names = ("grok",)
    title_hints = ("grok",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        session = _session_process(ctx.pane_pid)
        if session:
            turn_number, complete = _turn_state(session[2], session[1])
            if turn_number is not None:
                return AdapterResult("IDLE" if complete else "WORKING", "grok-turn-events")

        tail = ctx.tail(20)

        if _RUNNING_RE.search(tail):
            return AdapterResult("WORKING", "loop-running")

        if _IDLE_HINT_RE.search(tail):
            return AdapterResult("IDLE", "send-a-message-hint")

        return AdapterResult(None)

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        # Full capture, not ctx.tail(20) -- the task list can sit well
        # above the bottom input box with blank space in between.
        full = "\n".join(ctx.lines)
        matches = _TASK_RE.findall(full)
        if not matches:
            return None
        return Activity(text=matches[-1].strip(), confidence="medium", source="agent-output")

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        session = _session_process(ctx.pane_pid)
        if not session:
            return None
        turn_number, complete = _turn_state(session[2], session[1])
        if turn_number is None or not complete:
            return None
        return _export_result(session, turn_number)

    def turn_watermark(self, ctx: PaneContext) -> Optional[Tuple[str, int]]:
        session = _session_process(ctx.pane_pid)
        if not session:
            return None
        turn_number, _complete = _turn_state(session[2], session[1])
        return (session[1], turn_number) if turn_number is not None else (session[1], -1)

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        del text  # The event boundary confirms submission; the prompt body is not retained.
        if before.turn_watermark is None or after.turn_watermark is None:
            return None
        old_session, old_turn = before.turn_watermark
        new_session, new_turn = after.turn_watermark
        if old_session != new_session:
            return None
        return new_turn > old_turn
