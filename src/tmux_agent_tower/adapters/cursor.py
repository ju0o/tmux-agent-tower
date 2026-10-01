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
    looks_like_generic_waiting,
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

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        if _FOLLOWUP_RE.search(tail):
            return AdapterResult("IDLE", "add-a-follow-up")

        return AdapterResult(None)

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        # "Add a follow-up" also sits on screen while a turn is still
        # running ("ctrl+c to stop"). That is not a final answer.
        lines = list(ctx.lines)
        full = "\n".join(lines)
        if re.search(r"ctrl\+c to stop", full, re.IGNORECASE):
            return None
        if not _FOLLOWUP_RE.search(ctx.tail(20)):
            return None
        end = max(i for i, line in enumerate(lines) if _FOLLOWUP_RE.search(line))
        text = prose_body(lines[:end])
        if text is None:
            return None
        return ResultCandidate(text=text, fingerprint=result_fingerprint(text))
