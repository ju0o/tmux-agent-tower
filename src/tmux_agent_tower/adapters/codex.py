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

from .base import (
    AgentAdapter,
    AdapterResult,
    PaneContext,
    BRAILLE_SPINNER_CHARS,
    looks_like_generic_waiting,
    title_has_spinner,
)

_WORKING_RE = re.compile(r"\bworking\s*\([^)]*esc to interrupt", re.IGNORECASE)
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
