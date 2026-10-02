"""Cursor Agent CLI adapter.

Evidence: Cursor's CLI shows completed tool calls as ``Finished <tool>``
lines and a status bar like ``Auto Balance · 32.6% · 4 files edited``. The
most reliable *turn is over* signal observed is a ``→ Add a follow-up``
prompt, which only appears once Cursor is ready for the next instruction.
There was no unambiguous "currently generating" text captured, so active
work is mostly detected via the generic output-hash-change signal in the
status engine; this adapter mainly narrows down IDLE vs WAITING.
"""

from __future__ import annotations

import re

from .base import (
    AgentAdapter,
    AdapterResult,
    PaneContext,
    ResultCandidate,
    prose_body,
    result_fingerprint,
)

_FOLLOWUP_RE = re.compile(r"add a follow-up", re.IGNORECASE)


class CursorAdapter(AgentAdapter):
    name = "Cursor"
    # tmux reports cursor-agent's #{pane_current_command} as just "agent";
    # "cursor-agent" is matched against the full process command line
    # instead (see AgentAdapter.matches), since "agent" alone is far too
    # generic to use as a short-command match.
    command_names = ("cursor-agent",)
    title_hints = ("cursor",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _FOLLOWUP_RE.search(tail):
            return AdapterResult("IDLE", "add-a-follow-up")

        return AdapterResult(None)

    def _widget(self, ctx: PaneContext) -> str:
        return "\n".join(ctx.lines[-8:])

    def detect_attention(self, ctx: PaneContext) -> str:
        widget = self._widget(ctx)
        if re.search(r"ctrl\+c to stop", widget, re.IGNORECASE) or _FOLLOWUP_RE.search(widget):
            return "none"
        if re.search(r"run this command\?|allow\?", widget, re.IGNORECASE) and re.search(r"\(y/n\)", widget, re.IGNORECASE):
            return "approval_required"
        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        from ..detection.attention import clamp_prompt

        if self.detect_attention(ctx) == "none":
            return ""
        for line in reversed(self._widget(ctx).split("\n")):
            if "?" in line and "(y/n)" not in line.lower():
                return clamp_prompt(line.strip())
        return ""

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        # "Add a follow-up" also sits on screen while a turn is still
        # running ("ctrl+c to stop"). That is not a final answer.
        lines = list(ctx.lines)
        if not _FOLLOWUP_RE.search(ctx.tail(20)):
            return None
        ends = [i for i, line in enumerate(lines) if _FOLLOWUP_RE.search(line)]
        end = ends[-1]
        if any(re.search(r"ctrl\+c to stop", line, re.IGNORECASE) for line in lines[end:]):
            return None
        floor = ends[-2] + 1 if len(ends) > 1 else 0
        if len(ends) < 2:
            floor = max(floor, end - 60)
        text = prose_body(lines[floor:end])
        if text is None:
            return None
        return ResultCandidate(text=text, fingerprint=result_fingerprint(text))
