from .base import AgentAdapter, AdapterResult, PaneContext
from .codex import CodexAdapter
from .claude import ClaudeAdapter
from .opencode import OpenCodeAdapter
from .grok import GrokAdapter
from .cursor import CursorAdapter
from .shell import ShellAdapter

ADAPTERS = [
    CodexAdapter(),
    ClaudeAdapter(),
    OpenCodeAdapter(),
    GrokAdapter(),
    CursorAdapter(),
    ShellAdapter(),
]


def adapter_named(name: str) -> AgentAdapter:
    """The adapter whose display name is ``name``, or the shell fallback."""

    for adapter in ADAPTERS:
        if adapter.name == name:
            return adapter
    return ShellAdapter.UNKNOWN_FALLBACK


def resolve_adapter(command: str, title: str, cmdline: str = "", lines=()) -> AgentAdapter:
    """Pick an adapter from process evidence before the pane title.

    ``matches()`` still exists for a single adapter's own check, but the
    walk here does not let a title hint outrank an executable. A verified
    shell stays a shell even when the title says Claude.
    """

    from ..detection.identity import identify_agent

    name, _source = identify_agent(command, title, cmdline, lines)
    for adapter in ADAPTERS:
        if adapter.name == name:
            return adapter
    return ShellAdapter.UNKNOWN_FALLBACK


__all__ = [
    "AgentAdapter",
    "AdapterResult",
    "PaneContext",
    "adapter_named",
    "ADAPTERS",
    "resolve_adapter",
]
