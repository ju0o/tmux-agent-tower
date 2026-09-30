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
from typing import Optional

from .base import Activity, AgentAdapter, AdapterResult, PaneContext, looks_like_generic_waiting

_WORKING_RE = re.compile(r"\besc interrupt\b", re.IGNORECASE)
_IDLE_HINT_RE = re.compile(r"ctrl\+p commands", re.IGNORECASE)
# "~ Preparing write…" -- a tool call actively being prepared/run.
_PREPARING_RE = re.compile(r"^\s*~\s*(.+?)\s*$")
# "→ Read ." -- a tool invocation already dispatched (may be finished by
# the time this is read, hence lower confidence than the "~" form).
_ARROW_RE = re.compile(r"^\s*→\s*(.+?)\s*$")


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

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        # Deliberately the FULL capture, not ctx.tail(20): a short response
        # can leave a lot of blank padding between the activity/tool-call
        # text and the bottom status bar, pushing it out of the last 20
        # lines (caught live via a fixture with exactly this shape).
        lines = ctx.lines
        full = "\n".join(lines)

        for line in reversed(lines):
            m = _PREPARING_RE.match(line)
            if m:
                return Activity(text=m.group(1), confidence="high", source="agent-output")

        # A "→ Tool" line by itself is NOT reliable evidence of *current*
        # activity: it stays on screen after the turn finishes too (caught
        # live -- the idle fixture is literally a finished turn that still
        # shows its own "→ Read ." line). Only trust it as a fallback when
        # the working indicator confirms we're still actually mid-turn.
        if _WORKING_RE.search(full):
            for line in reversed(lines):
                m = _ARROW_RE.match(line)
                if m:
                    return Activity(text=m.group(1), confidence="medium", source="agent-output")

        return None
