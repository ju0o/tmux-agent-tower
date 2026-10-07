"""Claude Code adapter.

Evidence: Claude Code cycles a spinner glyph (``✳``, ``✻``, ``✽``, ``*``, ...)
at the start of a status line while a turn is running, paired with an
active verb and an elapsed-time/token counter, e.g. ``* Synthesizing… (1m
24s · ↓ 6.3k tokens)``. Past-tense summaries such as ``✻ Cooked for 32m
41s`` describe a *finished* turn, not an active one, so the verb tense
matters: ``-ing`` + elapsed timer = working, past tense = not working.

The bottom hint bar (``esc to interrupt · ...``) is shown even when Claude
is idle and simply waiting at the ``❯`` prompt in this build, so it is
deliberately NOT used as a working signal on its own.
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
    CLAUDE_SPINNER_CHARS,
    count_matches,
    count_prompt_echoes,
    prose_body,
    result_fingerprint,
    title_has_spinner,
)

# e.g. "Synthesizing… (1m 24s", "Cooking for 32m" (still -ing + running),
# deliberately requires the "-ing" verb form immediately before the ellipsis
# or opening paren, which past-tense summaries ("Cooked for ...") do not have.
_ACTIVE_VERB_RE = re.compile(r"\b[A-Za-z]+ing[…\.]{0,3}\s*\(", re.UNICODE)
# Same shape, but capturing just the verb for activity text.
_ACTIVE_VERB_CAPTURE_RE = re.compile(r"\b([A-Za-z]+ing)[…\.]{0,3}\s*\(", re.UNICODE)
# Claude lists completed actions as bullet lines, e.g. "• Read src/example.py".
_BULLET_RE = re.compile(r"^\s*•\s*(.+?)\s*$")

# The prompt box is its own line; a hint bar (which may itself contain
# "esc to interrupt") is typically drawn below it, so this must NOT require
# the prompt to be the very last non-blank line -- just present near the
# bottom of the visible tail.
_EMPTY_PROMPT_RE = re.compile(r"^\s*[❯>]\s*$", re.MULTILINE)
# Claude Code 2.1 shows a placeholder suggestion in the empty box, e.g.
# ❯ Try "fix typecheck errors". The pane title is a static "✳ Claude Code"
# while idle, so the title glyph is not working evidence for this build.
_PLACEHOLDER_PROMPT_RE = re.compile(r"^\s*[❯>]\s+Try \"", re.MULTILINE)
# Idle footers seen live: "⏸ manual mode on · ? for shortcuts" and
# "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents". While a turn
# runs the footer switches to "esc to interrupt" and the active verb wins.
_IDLE_FOOTER_RE = re.compile(r"\? for shortcuts|← for agents|shift\+tab to cycle", re.IGNORECASE)
# Finished-turn summary, e.g. "✻ Sautéed for 2s · done 5:19 PM" or
# "✻ Cooked for 32m 41s". A new one after a send proves the turn ran.
_DONE_LINE_RE = re.compile(r"\b[A-Za-zé]+ed for \d+[smh]", re.IGNORECASE)
# Same summary but anchored to the line start (optionally after the
# spinner glyph) so a sentence inside the answer body cannot end a turn.
# The verb is random per turn (Cooked/Brewed/Crunched/Sautéed/Worked...).
_RESULT_DONE_RE = re.compile(
    r"^\s*(?:[✻✳✶✽✢·*]\s+)?[A-Za-zé]+ed for \d+[smh]",
    re.IGNORECASE | re.MULTILINE,
)
_PROCEED_RE = re.compile(r"do you want to proceed\?", re.IGNORECASE)
# Folder trust picker. The highlighted default observed live is "No, exit".
# There is no numbered key on that screen, so approve() stays None.
_TRUST_Q_RE = re.compile(r"is this a project you created or one you trust\?", re.IGNORECASE)
_TRUST_YES_RE = re.compile(r"yes,\s+i trust this folder", re.IGNORECASE)
_OPTION_YES_RE = re.compile(r"^\s*1\.\s+yes\b", re.IGNORECASE | re.MULTILINE)
_OPTION_NO_RE = re.compile(r"^\s*3\.\s+no\b", re.IGNORECASE | re.MULTILINE)
_COOKED_RE = re.compile(r"\bcooked for\b", re.IGNORECASE)
_USER_LINE_RE = re.compile(r"^\s*[❯>]\s+\S")
_CLAUDE_PROMPT_RE = re.compile(r"^\s*❯", re.MULTILINE)
# A finished tool row in Claude Code when the cooked-for line has
# scrolled off. Live rows look like "◇ … 12.4s" or "◇ … 111.8k"
# and sit above the idle prompt. Extract only uses this once the
# widget is already idle.
_DIAMOND_ELAPSED_RE = re.compile(r"^\s*◇\s+\S")


def _spinner_title(ctx: PaneContext) -> bool:
    """A glyph plus a verb in the title (older builds).

    Measured on Claude Code 2.1.283: the title is "✳ Claude Code" before
    the first turn and "✳ <conversation title>" afterwards, and it did not
    change at all while a turn ran. A leading ✳ therefore says nothing
    about execution; only the other glyphs keep their older meaning.
    """

    title = (ctx.title or "").strip()
    if not title_has_spinner(title, CLAUDE_SPINNER_CHARS):
        return False
    if title.startswith("✳"):
        return False
    return title.lower() not in {"✻ claude code", "* claude code"}


class ClaudeAdapter(AgentAdapter):
    name = "Claude"
    command_names = ("claude",)
    title_hints = ("claude",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        active_at, idle_at = self._signals(ctx)
        # The lower signal is the current widget. An older "ing (" line
        # still inside the window does not beat the idle footer below it.
        if idle_at >= 0 and idle_at > active_at:
            return AdapterResult("IDLE", "idle-widget")
        if active_at >= 0:
            return AdapterResult("WORKING", "active-verb")
        return AdapterResult(None)

    def _signals(self, ctx: PaneContext) -> tuple:
        widget = self._widget(ctx)
        active_at = self._last_at(widget, _ACTIVE_VERB_RE)
        idle_at = max(
            self._last_at(widget, _EMPTY_PROMPT_RE),
            self._last_at(widget, _PLACEHOLDER_PROMPT_RE),
        )
        if not _spinner_title(ctx):
            idle_at = max(idle_at, self._last_at(widget, _IDLE_FOOTER_RE))
        if _CLAUDE_PROMPT_RE.search(widget):
            idle_at = max(idle_at, self._last_at(widget, _RESULT_DONE_RE))
        return active_at, idle_at

    @staticmethod
    def _last_at(text: str, pattern) -> int:
        found = -1
        for match in pattern.finditer(text):
            found = match.start()
        return found

    def _idle_widget(self, ctx: PaneContext) -> bool:
        """The bottom box is ready for input, in English or not.

        A localized footer still draws the shift+tab hint. A finished
        turn also leaves the cooked-for line above the prompt. An older
        active-verb line above that box is not the current turn.
        """

        active_at, idle_at = self._signals(ctx)
        return idle_at >= 0 and idle_at > active_at

    def _widget(self, ctx: PaneContext) -> str:
        return "\n".join(ctx.lines[-18:])

    def detect_attention(self, ctx: PaneContext) -> str:
        widget = self._widget(ctx)
        approval_at = -1
        if _PROCEED_RE.search(widget) and _OPTION_YES_RE.search(widget):
            approval_at = max(
                self._last_at(widget, _PROCEED_RE),
                self._last_at(widget, _OPTION_YES_RE),
            )
        if _TRUST_Q_RE.search(widget) and _TRUST_YES_RE.search(widget):
            approval_at = max(
                approval_at,
                self._last_at(widget, _TRUST_Q_RE),
                self._last_at(widget, _TRUST_YES_RE),
            )

        lines = widget.split("\n")
        input_at = -1
        empty_prompt_at = self._last_at(widget, _EMPTY_PROMPT_RE)
        if empty_prompt_at >= 0:
            for index, line in enumerate(lines):
                if index < empty_prompt_at and line.strip().endswith("?") and not _USER_LINE_RE.match(line):
                    input_at = index

        attention_at = max(approval_at, input_at)
        current_at = max(
            self._last_at(widget, _ACTIVE_VERB_RE),
            self._last_at(widget, _COOKED_RE),
        )
        if attention_at >= 0 and current_at <= attention_at:
            if approval_at >= input_at:
                return "approval_required"
            return "input_required"
        return "none"

    def extract_attention_prompt(self, ctx: PaneContext) -> str:
        from ..detection.attention import clamp_prompt

        if self.detect_attention(ctx) == "none":
            return ""
        widget = self._widget(ctx)
        if _TRUST_Q_RE.search(widget):
            return clamp_prompt("Is this a project you created or one you trust?")
        if _PROCEED_RE.search(widget):
            return clamp_prompt("Do you want to proceed?")
        for line in reversed(widget.split("\n")):
            stripped = line.strip()
            if stripped.endswith("?"):
                return clamp_prompt(stripped)
        return ""

    def detect_interaction(self, ctx: PaneContext):
        from ..detection.interaction import InteractionOption, approval, text_question, unknown

        attention = self.detect_attention(ctx)
        if attention == "none":
            return None
        widget = self._widget(ctx)
        prompt = self.extract_attention_prompt(ctx)
        if _PROCEED_RE.search(widget) and _OPTION_YES_RE.search(widget):
            options = [InteractionOption("예", "1")]
            if _OPTION_NO_RE.search(widget):
                options.append(InteractionOption("아니오", "3"))
            return approval(prompt, options, "claude-proceed")
        if attention == "approval_required":
            # Folder trust has no numbered key on the captured screen.
            return unknown(prompt, "claude-unmapped")
        if attention == "input_required":
            return text_question(prompt, "claude-question")
        return None

    def approve(self, ctx: PaneContext) -> Optional[str]:
        if self.detect_attention(ctx) != "approval_required":
            return None
        if _OPTION_YES_RE.search(self._widget(ctx)):
            return "1"
        return None

    def reject(self, ctx: PaneContext) -> Optional[str]:
        if self.detect_attention(ctx) != "approval_required":
            return None
        if _OPTION_NO_RE.search(self._widget(ctx)):
            return "3"
        return None

    def confirm_submitted(self, before: PaneContext, after: PaneContext, text: str) -> Optional[bool]:
        # Measured on Claude Code 2.1: the pasted text sits in the box as
        # "❯ <text>". After Enter the echo stays in the transcript, the
        # box returns to empty or its placeholder, and a short turn ends
        # with a new "<Verb>ed for Ns" line within two seconds.
        was_working = self.detect_execution(before) == "WORKING"
        is_working = self.detect_execution(after) == "WORKING"
        if is_working and not was_working:
            return True
        echoed = count_prompt_echoes(after.lines, _USER_LINE_RE, text) > count_prompt_echoes(
            before.lines, _USER_LINE_RE, text
        )
        if not echoed:
            return None
        if count_matches(after.lines, _DONE_LINE_RE) > count_matches(before.lines, _DONE_LINE_RE):
            return True
        box_is_free = bool(_EMPTY_PROMPT_RE.search(after.tail(8)) or _PLACEHOLDER_PROMPT_RE.search(after.tail(8)))
        if box_is_free:
            return True
        return None

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        # Full capture, not ctx.tail(20): a short reply can leave enough
        # blank padding that the relevant line falls outside the last 20
        # (caught live against a similarly-shaped OpenCode fixture).
        full = "\n".join(ctx.lines)

        verb_match = _ACTIVE_VERB_CAPTURE_RE.search(full)
        if verb_match:
            return Activity(text=verb_match.group(1), confidence="high", source="agent-output")

        bullets = [m.group(1) for line in ctx.lines if (m := _BULLET_RE.match(line))]
        if bullets:
            # A completed-action bullet with no active spinner nearby might
            # be stale (Claude could be idle by now) -- shown, but flagged
            # lower-confidence rather than implied as "happening right now".
            return Activity(text=bullets[-1], confidence="low", source="agent-output")

        return None

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        lines = list(ctx.lines)
        if not self._idle_widget(ctx):
            return None
        cooked = [
            i for i, line in enumerate(lines)
            if _RESULT_DONE_RE.search(line) or _DIAMOND_ELAPSED_RE.search(line)
        ]
        if not cooked:
            return None
        end = cooked[-1]
        if _ACTIVE_VERB_RE.search("\n".join(lines[end:])) or _spinner_title(ctx):
            return None
        floor = cooked[-2] + 1 if len(cooked) > 1 else 0
        # Only the last turn: everything after the user's own ❯ line that
        # precedes the done summary. Older turns stay above the previous marker.
        start = floor
        found_user = False
        for i in range(end - 1, floor - 1, -1):
            if _USER_LINE_RE.match(lines[i]):
                start = i + 1
                found_user = True
                break
        bounded = found_user or len(cooked) > 1
        if not bounded:
            start = max(start, end - 60)
        text = prose_body(lines[start:end])
        if text is None:
            return None
        confidence = "high" if bounded else "partial"
        return ResultCandidate(
            text=text,
            fingerprint=result_fingerprint(text),
            confidence=confidence,
            complete=bounded,
        )
