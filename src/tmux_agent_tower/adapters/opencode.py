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

from .base import (
    Activity,
    AgentAdapter,
    AdapterResult,
    PaneContext,
    ResultCandidate,
    count_matches,
    prompt_head,
    prose_body,
    result_fingerprint,
)

# "▣  Build · <model> · 5.2s" closes a turn.
_TURN_DONE_RE = re.compile(r"▣.*\d+(?:\.\d+)?s\b")

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
        # OpenCode runs on the alternate screen, so the whole capture is
        # the current frame. Its welcome layout centers the prompt box,
        # which put the idle hint 41 rows above the bottom of a tall pane
        # (caught live). The status region is one widget either way.
        full = "\n".join(ctx.lines)

        if _WORKING_RE.search(full):
            return AdapterResult("WORKING", "interrupt-hint")

        if _IDLE_HINT_RE.search(full):
            return AdapterResult("IDLE", "commands-hint-no-interrupt")

        return AdapterResult(None)

    def _widget(self, ctx: PaneContext) -> str:
        return "\n".join(ctx.lines[-8:])

    def detect_attention(self, ctx: PaneContext) -> str:
        # The live approval widget was never captured. This matches only the
        # synthesized fixture shape, and approve() stays None on purpose.
        widget = self._widget(ctx)
        if _WORKING_RE.search(widget) or _IDLE_HINT_RE.search(widget):
            return "none"
        if re.search(r"allow this tool call\?", widget, re.IGNORECASE) and re.search(r"\(y/n\)", widget, re.IGNORECASE):
            return "approval_required"
        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        from ..detection.attention import clamp_prompt

        if self.detect_attention(ctx) == "none":
            return ""
        for line in reversed(self._widget(ctx).split("\n")):
            if "?" in line:
                return clamp_prompt(line.strip())
        return ""

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        # Measured on OpenCode 1.18: the submitted message is drawn as a
        # "┃  <text>" block and "esc interrupt" appears while it runs; a
        # finished turn adds a "▣ … Ns" line.
        full = "\n".join(after.lines)
        if _WORKING_RE.search(full):
            return True
        if count_matches(after.lines, _TURN_DONE_RE) > count_matches(before.lines, _TURN_DONE_RE):
            return True
        head = prompt_head(text)
        if head and any(line.strip().startswith("┃") and head in line for line in after.lines):
            return True
        return None

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

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        lines = list(ctx.lines)
        full = "\n".join(lines)
        if _WORKING_RE.search(full):
            return None
        if not _IDLE_HINT_RE.search(ctx.tail(8)):
            return None
        marks = [
            i for i, line in enumerate(lines)
            if "▣" in line and re.search(r"\d+(?:\.\d+)?s\b", line)
        ]
        if not marks:
            return None
        text = prose_body(lines[: marks[-1]])
        if text is None:
            return None
        return ResultCandidate(text=text, fingerprint=result_fingerprint(text))
