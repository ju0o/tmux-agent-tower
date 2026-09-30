"""Base adapter contract and shared, generic detection helpers.

Adapters give *opinions*, not final answers. Each adapter looks at a pane's
title/command/captured content and returns a status hint (or ``None`` when it
has no opinion). The status engine (see ``detection/status.py``) combines
that hint with generic signals such as output-hash change detection and
applies hysteresis. This keeps agent-specific pattern matching isolated so a
single wrong regex cannot silently corrupt every other agent's status.
"""

from __future__ import annotations

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


def looks_like_generic_waiting(text: str) -> bool:
    return bool(_GENERIC_WAIT_RE.search(text))


def title_has_spinner(title: str, chars: set) -> bool:
    stripped = (title or "").lstrip()
    return bool(stripped) and stripped[0] in chars
