"""Base adapter contract and shared, generic detection helpers.

Adapters give *opinions*, not final answers. Each adapter looks at a pane's
title/command/captured content and returns a status hint (or ``None`` when it
has no opinion). The status engine (see ``detection/status.py``) combines
that hint with generic signals such as output-hash change detection and
applies hysteresis. This keeps agent-specific pattern matching isolated so a
single wrong regex cannot silently corrupt every other agent's status.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

# Braille spinner frames used by several Node/Ink-based CLIs (Codex, and
# others built on similar spinner libraries).
BRAILLE_SPINNER_CHARS = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

# Spinner glyphs observed in Claude Code's own status line.
CLAUDE_SPINNER_CHARS = set("✳✻✽∗*·")

GENERIC_WAIT_PATTERNS = [
    # Deliberately NOT a bare r"\bapprove\b": several agents show a static
    # mode label like "always-approve" in their idle UI chrome, which is
    # not a question. Only an actual "approve?"-shaped prompt counts.
    r"\bapprove\s*\?",
    r"\bapproval\b",
    r"permission required",
    r"allow this",
    r"\ballow\b.*\?",
    r"\bconfirm\b",
    r"continue\?",
    r"press enter",
    r"enter to continue",
    r"\by\s*/\s*n\b",
    r"\byes\s*/\s*no\b",
    r"do you want",
    r"would you like",
    r"trust this (folder|directory|project|repo)",
    r"waiting for input",
    r"choose an option",
    r"select an option",
    r"^\s*\d+\.\s+(yes|no)\b",
]

_GENERIC_WAIT_RE = re.compile("|".join(GENERIC_WAIT_PATTERNS), re.IGNORECASE | re.MULTILINE)

SHELL_PROMPT_RE = re.compile(r"[\$#%>]\s*$")


@dataclass(frozen=True)
class PaneContext:
    """Everything an adapter needs to classify a single pane observation."""

    title: str
    command: str
    lines: Sequence[str] = field(default_factory=tuple)

    def tail(self, n: int = 15) -> str:
        return "\n".join(self.lines[-n:])

    def last_nonblank(self) -> str:
        for line in reversed(self.lines):
            if line.strip():
                return line.strip()
        return ""


@dataclass(frozen=True)
class AdapterResult:
    """An adapter's opinion. ``status`` is ``None`` when it has no opinion."""

    status: Optional[str] = None
    reason: str = ""


_NO_OPINION = AdapterResult(status=None)


@dataclass(frozen=True)
class ResultCandidate:
    """A final answer body. Idle time is not evidence: adapters return
    this only when their own completion UI is on screen and a prose
    body remains after chrome is removed. ``fingerprint`` identifies
    the text so the same answer is not announced again.
    """

    text: str
    fingerprint: str
    confidence: str = "high"


_CHROME_LINE = re.compile(
    r"^(?:"
    r"worked for\b|cooked for\b|esc to interrupt\b|ctrl\+c to stop\b|"
    r"ctrl\+p commands\b|ask codex\b|add a follow-up\b|tab to queue\b|"
    r"send a message\b|auto balance\b|"
    r"[•∙]\s*(?:finished|explored|called|working|read)\b|"
    r"finished\b|empty\s+[—-]|^\s*[→~]"
    r")",
    re.IGNORECASE,
)
_ONLY_COMPLETION_WORD = re.compile(
    r"^(?:finished|done|complete|completed|작업끝)[.!]?\s*$",
    re.IGNORECASE,
)
_BOX_ONLY = re.compile(r"^[\s─┃▀▁▂▃▄▅▆▇█░▒▓·•│╭╮╯╰▶═\-_=]+$")


def result_fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()[:16]


def prose_body(lines: Sequence[str]) -> Optional[str]:
    """Drop tool logs, status chrome, and a body that is only the word
    "Done"/"Finished". Returns None when nothing answer-shaped remains.
    """

    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _BOX_ONLY.match(stripped) or _CHROME_LINE.match(stripped):
            continue
        kept.append(stripped)
    text = "\n".join(kept).strip()
    if len(text) < 8 or _ONLY_COMPLETION_WORD.match(text):
        return None
    return text


@dataclass(frozen=True)
class Activity:
    """A best-effort, one-line description of what an agent looks like
    it's doing right now, extracted purely from terminal output patterns
    -- never from an LLM call (see docs/STATUS_ENGINE.md's "Task
    Awareness" section). ``confidence`` is informational only today
    ("high"/"medium"/"low"); the UI currently shows any non-None activity
    the same way, but keeping it lets a future version hide low-confidence
    guesses instead of showing a plausible-sounding wrong answer.

    Never persisted to disk: an activity string is a fragment of the
    pane's own terminal output, which can contain private project
    content, so it only ever exists in memory for the current refresh
    -- see ``state/`` for what Tower *does* persist (never this).
    """

    text: str
    confidence: str = "medium"
    source: str = "agent-output"


class AgentAdapter:
    """Default adapter: identifies nothing, classifies with generic rules only."""

    name = "Agent"
    command_names: Sequence[str] = ()
    title_hints: Sequence[str] = ()

    def matches(self, command: str, title: str, cmdline: str = "") -> bool:
        cmd = (command or "").strip().lower()
        low_title = (title or "").strip().lower()
        low_cmdline = (cmdline or "").strip().lower()

        for needle in self.command_names:
            if cmd == needle or cmd.startswith(needle):
                return True
            # tmux's #{pane_current_command} is often a generic wrapper name
            # (e.g. cursor-agent's binary reports as just "agent"), so also
            # check the full process command line when available.
            if needle in low_cmdline:
                return True

        for hint in self.title_hints:
            if hint in low_title:
                return True

        return False

    def classify(self, ctx: PaneContext) -> AdapterResult:
        return _NO_OPINION

    def extract_activity(self, ctx: PaneContext) -> Optional[Activity]:
        """What does this pane's output say the agent is doing right now?

        Returns ``None`` when there isn't enough evidence -- never
        fabricates a plausible-sounding guess. The default (this base
        implementation) never has an opinion; only agents with a verified,
        real-capture-based pattern override it.
        """

        return None

    def extract_result(self, ctx: PaneContext) -> Optional[ResultCandidate]:
        """Final answer body, or None. Silence is not a result. The
        default has no final-response concept (shells and unknown
        commands stay None).
        """

        return None


def looks_like_generic_waiting(text: str) -> bool:
    return bool(_GENERIC_WAIT_RE.search(text))


def title_has_spinner(title: str, chars: set) -> bool:
    stripped = (title or "").lstrip()
    return bool(stripped) and stripped[0] in chars
