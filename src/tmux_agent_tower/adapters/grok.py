"""Grok CLI adapter.

Evidence: Grok CLI draws a boxed input prompt at the bottom
(``╭─...─╮`` / ``❯`` / ``╰─ Grok ... ─╯``). While a background task/loop is
still active it shows a status line such as ``1 loop still running`` above
the box; when idle the same area reads ``send a message`` with nothing
running.
"""

from __future__ import annotations

import re

from .base import AgentAdapter, AdapterResult, PaneContext, looks_like_generic_waiting

_RUNNING_RE = re.compile(r"loop[s]?\s+(is\s+|are\s+)?still running", re.IGNORECASE)
_IDLE_HINT_RE = re.compile(r"send a message", re.IGNORECASE)


class GrokAdapter(AgentAdapter):
    name = "Grok"
    command_names = ("grok",)
    title_hints = ("grok",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _RUNNING_RE.search(tail):
            return AdapterResult("WORKING", "loop-running")

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        if _IDLE_HINT_RE.search(tail):
            return AdapterResult("IDLE", "send-a-message-hint")

        return AdapterResult(None)
