"""OpenCode adapter.

No live OpenCode session was available to sample during initial
development, so this adapter is intentionally conservative: it only relies
on generic waiting-prompt detection and a couple of commonly-seen
Ink/React-TUI conventions (spinner + "esc to interrupt", empty prompt
box). It is expected to be refined against real captures — see
``fixtures/opencode-*.txt`` and the "best-effort" note in the README.
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

_WORKING_RE = re.compile(r"\besc to interrupt\b", re.IGNORECASE)


class OpenCodeAdapter(AgentAdapter):
    name = "OpenCode"
    command_names = ("opencode",)
    title_hints = ("opencode",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS) and _WORKING_RE.search(tail):
            return AdapterResult("WORKING", "spinner-and-interrupt-hint")

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        return AdapterResult(None)
