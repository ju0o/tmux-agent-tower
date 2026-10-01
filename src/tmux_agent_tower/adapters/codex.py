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
    ResultCandidate,
    BRAILLE_SPINNER_CHARS,
    prompt_head,
    prose_body,
    result_fingerprint,
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
_ALLOW_RE = re.compile(r"allow this command to run\?", re.IGNORECASE)
_WOULD_RUN_RE = re.compile(r"would you like to run the following command\?", re.IGNORECASE)
_YES_PROCEED_RE = re.compile(r"1\.\s+yes,\s+proceed\b", re.IGNORECASE)
_TRUST_RE = re.compile(r"trust this folder\?", re.IGNORECASE)
_OPTION_YES_RE = re.compile(r"^\s*[›>]?\s*1\.\s+yes\b", re.IGNORECASE | re.MULTILINE)
_OPTION_TRUST_RE = re.compile(r"^\s*[›>]?\s*1\.\s+trust\b", re.IGNORECASE | re.MULTILINE)
_OPTION_NO_RE = re.compile(r"^\s*[›>]?\s*3\.\s+no\b", re.IGNORECASE | re.MULTILINE)
_USER_LINE_RE = re.compile(r"^\s*[›>]\s+\S")
_WORKED_FOR_RE = re.compile(r"\bworked for\b", re.IGNORECASE)
# Live trust widget (Codex CLI 0.159): the digit does not select the row.
# The footer is the key binding: "enter continue · esc quit".
_ENTER_CONTINUE_RE = re.compile(r"enter continue", re.IGNORECASE)
_ESC_QUIT_RE = re.compile(r"esc quit", re.IGNORECASE)


class CodexAdapter(AgentAdapter):
    name = "Codex"
    command_names = ("codex",)
    title_hints = ("codex",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _WORKING_RE.search(tail) or title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS):
            return AdapterResult("WORKING", "working-line-or-spinner")

        if _IDLE_HINT_RE.search(tail):
            return AdapterResult("IDLE", "queue-message-hint")

        return AdapterResult(None)

    def _widget(self, ctx: PaneContext) -> str:
        """The current bottom widget, not older scrollback above it.

        Eighteen lines covers a command preview between the question and
        the numbered choices. Idle and working chrome in that window still
        suppress a menu that has already scrolled away.
        """

        return "\n".join(ctx.lines[-18:])

    def detect_attention(self, ctx: PaneContext) -> str:
        widget = self._widget(ctx)
        if _WORKING_RE.search(widget) or title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS):
            return "none"
        if _IDLE_HINT_RE.search(widget) or _WORKED_FOR_RE.search(widget):
            return "none"
        command_menu = _OPTION_YES_RE.search(widget) and (
            _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        )
        if command_menu or (_TRUST_RE.search(widget) and _OPTION_TRUST_RE.search(widget)):
            return "approval_required"
        last = ""
        for line in reversed(widget.split("\n")):
            if line.strip():
                last = line.strip()
                break
        if last.endswith("?") and not _USER_LINE_RE.match(last):
            return "input_required"
        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        from ..detection.attention import clamp_prompt

        if self.detect_attention(ctx) == "none":
            return ""
        widget = self._widget(ctx)
        if _TRUST_RE.search(widget):
            return clamp_prompt("Trust this folder?")
        if _WOULD_RUN_RE.search(widget):
            return clamp_prompt("Would you like to run the following command?")
        if _ALLOW_RE.search(widget) or _YES_PROCEED_RE.search(widget):
            return clamp_prompt("Allow this command to run?")
        for line in reversed(widget.split("\n")):
            stripped = line.strip()
            if stripped.endswith("?"):
                return clamp_prompt(stripped)
        return ""

    def approve(self, ctx: PaneContext) -> Optional[str]:
        if self.detect_attention(ctx) != "approval_required":
            return None
        widget = self._widget(ctx)
        if _OPTION_YES_RE.search(widget) and (
            _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        ):
            return "1"
        # Trust: only the footer binding. A bare "1" was sent at this widget
        # and the menu did not move.
        if _TRUST_RE.search(widget) and _ENTER_CONTINUE_RE.search(widget):
            return "Enter"
        return None

    def reject(self, ctx: PaneContext) -> Optional[str]:
        if self.detect_attention(ctx) != "approval_required":
            return None
        widget = self._widget(ctx)
        if _OPTION_NO_RE.search(widget) and (
            _ALLOW_RE.search(widget) or _WOULD_RUN_RE.search(widget) or _YES_PROCEED_RE.search(widget)
        ):
            return "3"
        if _TRUST_RE.search(widget) and _ESC_QUIT_RE.search(widget):
            return "Escape"
        return None

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        # Measured on Codex 0.159: a bracketed paste leaves "› <text>" in the
        # composer with no idle hint. Once Enter is taken the same line
        # stays as the transcript echo and either a Working line or the
        # finished "Worked for" + idle hint follows.
        tail = after.tail(30)
        if _WORKING_RE.search(tail) or title_has_spinner(after.title, BRAILLE_SPINNER_CHARS):
            return True
        head = prompt_head(text)
        echoed = bool(head) and any(
            line.strip().startswith("›") and head in line for line in after.lines
        )
        if echoed and (_IDLE_HINT_RE.search(tail) or _WORKED_FOR_RE.search(tail)):
            return True
        return None

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

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        # A finished turn shows "Worked for ..." and the idle prompt.
        # "Finished" alone, or the same words inside a user prompt, is
        # not that evidence. A Working line means this screen is not final.
        lines = list(ctx.lines)
        full = "\n".join(lines)
        if _WORKING_RE.search(full) or title_has_spinner(ctx.title, BRAILLE_SPINNER_CHARS):
            return None
        if not _IDLE_HINT_RE.search(ctx.tail(15)):
            return None
        worked = [i for i, line in enumerate(lines) if re.search(r"worked for", line, re.IGNORECASE)]
        if not worked:
            return None
        end = worked[-1]
        start = 0
        for i in range(end - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("›") and "ask codex" not in stripped.lower():
                start = i + 1
                break
        text = prose_body(lines[start:end])
        if text is None:
            return None
        return ResultCandidate(text=text, fingerprint=result_fingerprint(text))
