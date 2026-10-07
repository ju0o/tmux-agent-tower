"""Cursor Agent CLI adapter.

Evidence: Cursor's CLI shows completed tool calls as ``Finished <tool>``
lines and a status bar like ``Auto Balance · 32.6% · 4 files edited``. The
most reliable *turn is over* signal observed is a ``→ Add a follow-up``
prompt, which only appears once Cursor is ready for the next instruction.
A generating turn keeps a separate footer, ``Running  4.99k tokens``
(also seen as ``12.47k``), above that prompt. Cursor Agent 2026.10 also
marks the same moment with a braille spinner and the word ``Working``
while ``Add a follow-up`` stays on screen. ``ctrl+c to stop`` on the
follow-up line itself is still there while that footer is visible, so
the running line is what marks the turn in progress. The empty composer
before the first turn reads ``Plan, search, build anything``.
"""

from __future__ import annotations

import re

from .base import (
    AgentAdapter,
    AdapterResult,
    PaneContext,
    ResultCandidate,
    count_matches,
    prompt_head,
    prose_body,
    result_fingerprint,
)

_FOLLOWUP_RE = re.compile(r"^\s*(?:→\s*)?add a follow-up\b", re.IGNORECASE | re.MULTILINE)
_STOP_RE = re.compile(r"ctrl\+c to stop", re.IGNORECASE)
# Captured above the follow-up prompt while the turn was still generating.
_RUNNING_TOKENS_RE = re.compile(r"\bRunning\s+\d+(?:\.\d+)?k\s+tokens\b", re.IGNORECASE)
# Cursor Agent 2026.10.01 draws a braille spinner and the word Working on
# its own line. "Add a follow-up" stays visible during that turn, so the
# follow-up line by itself is not idle. Measured in an isolated pane.
_SPINNER_WORKING_RE = re.compile(r"[\u2800-\u28FF]+\s+Working\b")
_TIP_RE = re.compile(r"^\s*Tip:", re.IGNORECASE)
# Empty composer on that build, before the first turn replaces it.
_PLAN_PROMPT_RE = re.compile(r"plan,\s*search,\s*build anything", re.IGNORECASE)


class CursorAdapter(AgentAdapter):
    name = "Cursor"
    # tmux reports cursor-agent's #{pane_current_command} as just "agent";
    # "cursor-agent" is matched against the full process command line
    # instead (see AgentAdapter.matches), since "agent" alone is far too
    # generic to use as a short-command match.
    command_names = ("cursor-agent",)
    title_hints = ("cursor",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        # Cursor's answer history can retain an old spinner above its live
        # composer. Use only the current bottom widget so that old work does
        # not override either a current spinner or an idle follow-up prompt.
        lines = self._widget(ctx).splitlines()
        work_lines = [
            i for i, line in enumerate(lines)
            if _RUNNING_TOKENS_RE.search(line) or _SPINNER_WORKING_RE.search(line)
        ]
        prompt_lines = [
            i for i, line in enumerate(lines)
            if _FOLLOWUP_RE.search(line) or _PLAN_PROMPT_RE.search(line)
        ]

        if work_lines:
            work_at = work_lines[-1]
            prompt_at = prompt_lines[-1] if prompt_lines else -1
            if prompt_at < 0 or work_at >= prompt_at:
                return AdapterResult("WORKING", "running-marker")
            # An answer below an older spinner, followed by the current
            # composer, proves the spinner is stale. A current stop footer
            # or a newly appearing spinner is handled separately by the
            # submission confirmation path.
            if any(_STOP_RE.search(line) for line in lines[prompt_at:]):
                return AdapterResult("WORKING", "running-marker")
            if any(
                line.strip() and not _TIP_RE.search(line)
                for line in lines[work_at + 1:prompt_at]
            ):
                return AdapterResult("IDLE", "completed-body-after-spinner")
            return AdapterResult("WORKING", "running-marker")

        if prompt_lines:
            reason = "add-a-follow-up" if _FOLLOWUP_RE.search(lines[prompt_lines[-1]]) else "plan-prompt"
            return AdapterResult("IDLE", reason)

        return AdapterResult(None)

    def _widget(self, ctx: PaneContext) -> str:
        return "\n".join(ctx.lines[-8:])

    def detect_interaction(self, ctx: PaneContext):
        from ..detection.interaction import confirm

        if self.detect_attention(ctx) != "approval_required":
            return None
        # "(y/n)" is visible, but this build was never measured for
        # whether the key is y alone or y then Enter.
        return confirm(self.extract_attention_prompt(ctx), None, None, "cursor-yn-unmeasured")

    def followup_submit_key(self, before: PaneContext, after: PaneContext, text: str) -> Optional[str]:
        if self.confirm_submitted(before, after, text) is True:
            return None
        if self.detect_execution(after) == "WORKING":
            return None
        head = prompt_head(text)
        lines = list(after.lines[-8:])
        followups = [i for i, line in enumerate(lines) if _FOLLOWUP_RE.search(line)]
        composer = [line.strip() for line in lines[followups[-1] + 1:] if line.strip()] if followups else []
        if head and composer and composer[0].startswith(head):
            return "Enter"
        return None

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        # Running confirms this prompt only when the marker appeared after
        # the send; an already visible marker is stale evidence.
        if count_matches(after.lines, _SPINNER_WORKING_RE) > count_matches(before.lines, _SPINNER_WORKING_RE):
            return True
        was_working = self.detect_execution(before) == "WORKING"
        if self.detect_execution(after) == "WORKING":
            return True if not was_working else None

        # A completed turn needs a new follow-up boundary and a result body.
        if count_matches(after.lines, _FOLLOWUP_RE) <= count_matches(before.lines, _FOLLOWUP_RE):
            return None
        result = self.extract_result(after)
        previous = self.extract_result(before)
        if result is not None and (previous is None or result.fingerprint != previous.fingerprint):
            return True
        return None

    def detect_attention(self, ctx: PaneContext) -> str:
        widget = self._widget(ctx)
        if re.search(r"run this command\?|allow\?", widget, re.IGNORECASE) and re.search(r"\(y/n\)", widget, re.IGNORECASE):
            lines = widget.split("\n")
            approval_at = max(i for i, line in enumerate(lines) if "?" in line or "(y/n)" in line.lower())
            current_at = max(
                [i for i, line in enumerate(lines) if _RUNNING_TOKENS_RE.search(line) or _SPINNER_WORKING_RE.search(line)]
                + [i for i, line in enumerate(lines) if _FOLLOWUP_RE.search(line) or _STOP_RE.search(line)]
                + [-1]
            )
            if current_at <= approval_at:
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
        if _RUNNING_TOKENS_RE.search(ctx.tail(20)) or _SPINNER_WORKING_RE.search(ctx.tail(20)):
            return None
        if not _FOLLOWUP_RE.search(ctx.tail(20)):
            return None
        ends = [i for i, line in enumerate(lines) if _FOLLOWUP_RE.search(line)]
        end = ends[-1]
        # "ctrl+c to stop" on the follow-up line itself is the prompt
        # chrome. A separate stop line below it means the turn is still running.
        if any(
            re.search(r"ctrl\+c to stop", line, re.IGNORECASE) and not _FOLLOWUP_RE.search(line)
            for line in lines[end:]
        ):
            return None
        bounded = len(ends) > 1
        floor = ends[-2] + 1 if bounded else max(0, end - 60)
        text = prose_body(lines[floor:end])
        # Tool chrome can fill the first 60 lines above a single follow-up.
        if text is None and not bounded:
            text = prose_body(lines[max(0, end - 200):end])
        if text is None:
            return None
        confidence = "high" if bounded else "partial"
        return ResultCandidate(
            text=text,
            fingerprint=result_fingerprint(text),
            confidence=confidence,
            complete=bounded,
            turn_complete=True,
            body_complete=bounded,
        )
