"""Codex CLI adapter.

Evidence gathered by capturing a live Codex session (see
``evidence/baseline/`` notes, sanitized into ``fixtures/codex-*.txt``):

* Actively working: a line shaped like ``Working (12m 3s • esc to
  interrupt)`` appears above the input box, and/or the pane title starts
  with a braille spinner frame (e.g. ``⠙ Prepare P14 direct pilot``).
* Idle / ready for input: the bottom input box (``›``) is empty and shows a
  ``tab to queue message`` hint with no ``Working (...)`` line above it.
* Waiting on the user: an explicit approval/confirmation prompt is shown
  (falls back to the generic waiting-pattern detector).
"""

from __future__ import annotations

import re

from typing import Optional

from .base import (
    Activity,
    AgentAdapter,
    AdapterResult,
    PaneContext,
    BRAILLE_SPINNER_CHARS,
    looks_like_generic_waiting,
    title_has_spinner,
)

_WORKING_RE = re.compile(r"\bworking\s*\([^)]*esc to interrupt", re.IGNORECASE)
# Codex lists its own steps as bullet lines, e.g. "• Explored",
# "• Called some_tool.run_query", "• Calling some_tool.get_thing" -- the
# present-tense "Calling ..." form specifically marks a tool call that's
# still in flight (evidence: a real captured session, see
# fixtures/codex-working.txt).
_BULLET_RE = re.compile(r"^\s*•\s*(.+?)\s*$")
_IN_PROGRESS_RE = re.compile(r"^calling\b", re.IGNORECASE)
# The bullet-shaped status/summary lines that are NOT an action
# description and must never be surfaced as "current activity":
# "• Working (...)" (that's the status line itself, not a task) and
# "• Finished ..." (a past-tense turn-complete summary, misleading if
# shown while idle).
_NOT_AN_ACTIVITY_RE = re.compile(r"^(working\s*\(|finished\b)", re.IGNORECASE)
# Shown only once the current turn is done and the input box is the
# placeholder-empty "Ask Codex to do anything" prompt. While a turn is
# still running the same box instead reads "tab to queue message", which is
# deliberately NOT treated as an idle signal.
_IDLE_HINT_RE = re.compile(r"ask codex to do anything|for agents.*for shortcuts", re.IGNORECASE)


class CodexAdapter(AgentAdapter):
    name = "Codex"
    command_names = ("codex",)
    title_hints = ("codex",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _WORKING_RE.search(tail) or title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS):
            return AdapterResult("WORKING", "working-line-or-spinner")

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        if _IDLE_HINT_RE.search(tail):
            return AdapterResult("IDLE", "queue-message-hint")

        return AdapterResult(None)

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        # Bullets are only trustworthy as "current activity" while there is
        # direct evidence we're still mid-turn (the "Working (... esc to
        # interrupt)" line). Once a turn finishes, Codex leaves its own
        # summary bullets sitting in scrollback right above the idle
        # "Ask Codex to do anything" prompt -- a naive "last bullet found
        # anywhere" scan would report that stale, completed summary as if
        # it were still happening (caught live against a real idle pane,
        # not a synthetic fixture: a "그럴듯한 오답").
        full = "\n".join(ctx.lines)
        if not _WORKING_RE.search(full):
            return None

        # Full capture, not ctx.tail(20): a short reply can leave enough
        # blank padding below it that the last real bullet line falls
        # outside the last 20 lines (caught live).
        bullets = [
            m.group(1)
            for line in ctx.lines
            if (m := _BULLET_RE.match(line)) and not _NOT_AN_ACTIVITY_RE.match(m.group(1))
        ]

        if not bullets:
            return None

        for text in reversed(bullets):
            if _IN_PROGRESS_RE.match(text):
                return Activity(text=text, confidence="high", source="agent-output")

        return Activity(text=bullets[-1], confidence="medium", source="agent-output")
