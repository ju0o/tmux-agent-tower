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
_PROCEED_RE = re.compile(r"do you want to proceed\?", re.IGNORECASE)
# Folder trust picker. The highlighted default observed live is "No, exit".
# There is no numbered key on that screen, so approve() stays None.
_TRUST_Q_RE = re.compile(r"is this a project you created or one you trust\?", re.IGNORECASE)
_TRUST_YES_RE = re.compile(r"yes,\s+i trust this folder", re.IGNORECASE)
_OPTION_YES_RE = re.compile(r"^\s*1\.\s+yes\b", re.IGNORECASE | re.MULTILINE)
_OPTION_NO_RE = re.compile(r"^\s*3\.\s+no\b", re.IGNORECASE | re.MULTILINE)
_COOKED_RE = re.compile(r"\bcooked for\b", re.IGNORECASE)
_USER_LINE_RE = re.compile(r"^\s*[❯>]\s+\S")


class ClaudeAdapter(AgentAdapter):
    name = "Claude"
    command_names = ("claude",)
    title_hints = ("claude",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _ACTIVE_VERB_RE.search(tail) or title_has_spinner(ctx.title, CLAUDE_SPINNER_CHARS):
            return AdapterResult("WORKING", "active-verb-or-spinner")

        if _EMPTY_PROMPT_RE.search(tail):
            return AdapterResult("IDLE", "empty-prompt")

        return AdapterResult(None)

    def _widget(self, ctx: PaneContext) -> str:
        return "\n".join(ctx.lines[-12:])

    def detect_attention(self, ctx: PaneContext) -> str:
        widget = self._widget(ctx)
        if _ACTIVE_VERB_RE.search(widget) or title_has_spinner(ctx.title, CLAUDE_SPINNER_CHARS):
            return "none"
        if _COOKED_RE.search(widget):
            return "none"
        if _PROCEED_RE.search(widget) and _OPTION_YES_RE.search(widget):
            return "approval_required"
        if _TRUST_Q_RE.search(widget) and _TRUST_YES_RE.search(widget):
            return "approval_required"
        last = ""
        for line in reversed(widget.split("\n")):
            if line.strip() and not _EMPTY_PROMPT_RE.match(line):
                last = line.strip()
                break
        if last.endswith("?") and not _USER_LINE_RE.match(last) and _EMPTY_PROMPT_RE.search(widget):
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
        full = "\n".join(lines)
        if _ACTIVE_VERB_RE.search(full) or title_has_spinner(ctx.title, CLAUDE_SPINNER_CHARS):
            return None
        if not _EMPTY_PROMPT_RE.search(ctx.tail(20)):
            return None
        cooked = [i for i, line in enumerate(lines) if re.search(r"\bCooked for\b", line)]
        if not cooked:
            return None
        text = prose_body(lines[: cooked[-1]])
        if text is None:
            return None
        return ResultCandidate(text=text, fingerprint=result_fingerprint(text))
