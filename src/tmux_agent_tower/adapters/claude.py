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

from .base import (
    AgentAdapter,
    AdapterResult,
    PaneContext,
    CLAUDE_SPINNER_CHARS,
    looks_like_generic_waiting,
    title_has_spinner,
)

# e.g. "Synthesizing… (1m 24s", "Cooking for 32m" (still -ing + running),
# deliberately requires the "-ing" verb form immediately before the ellipsis
# or opening paren, which past-tense summaries ("Cooked for ...") do not have.
_ACTIVE_VERB_RE = re.compile(r"\b[A-Za-z]+ing[…\.]{0,3}\s*\(", re.UNICODE)

# The prompt box is its own line; a hint bar (which may itself contain
# "esc to interrupt") is typically drawn below it, so this must NOT require
# the prompt to be the very last non-blank line -- just present near the
# bottom of the visible tail.
_EMPTY_PROMPT_RE = re.compile(r"^\s*[❯>]\s*$", re.MULTILINE)


class ClaudeAdapter(AgentAdapter):
    name = "Claude"
    command_names = ("claude",)
    title_hints = ("claude",)

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if _ACTIVE_VERB_RE.search(tail) or title_has_spinner(ctx.title, CLAUDE_SPINNER_CHARS):
            return AdapterResult("WORKING", "active-verb-or-spinner")

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        if _EMPTY_PROMPT_RE.search(tail):
            return AdapterResult("IDLE", "empty-prompt")

        return AdapterResult(None)
