"""OpenCode adapter.

Verified 2026-09-30 against a real, live OpenCode 1.18.32 session (see
``fixtures/opencode-working.txt`` / ``opencode-idle.txt``, sanitized):

* Actively working: the bottom status bar shows a block/dot progress
  indicator followed by ``esc interrupt`` (e.g. ``⬝⬝⬝⬝⬝⬝⬝⬝  esc interrupt``).
  This text is absent whenever OpenCode is idle — confirmed by comparing
  multiple idle captures before and after a real task.
* Idle / ready: bottom bar instead reads ``... ctrl+p commands`` (no
  ``esc interrupt``), and/or a just-finished summary line like
  ``▣  Build · <mode> · 6.0s`` (past tense — the turn is over).

NOT verified live: an explicit permission/confirmation prompt. The
session tested was configured in a mode that auto-approved file writes,
so no such prompt ever appeared on screen to capture. WAITING therefore
still falls back entirely to the generic waiting-pattern detector for
this adapter — treat that as an open item if OpenCode's own approval UI
turns out to need a specific pattern (see CONTRIBUTING.md).
"""

from __future__ import annotations

import re

from .base import AgentAdapter, AdapterResult, PaneContext, looks_like_generic_waiting

_WORKING_RE = re.compile(r"\besc interrupt\b", re.IGNORECASE)
_IDLE_HINT_RE = re.compile(r"ctrl\+p commands", re.IGNORECASE)


class OpenCodeAdapter(AgentAdapter):
    name = "OpenCode"
    command_names = ("opencode",)
    title_hints = ("opencode",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _WORKING_RE.search(tail):
            return AdapterResult("WORKING", "interrupt-hint")

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        if _IDLE_HINT_RE.search(tail):
            return AdapterResult("IDLE", "commands-hint-no-interrupt")

        return AdapterResult(None)
