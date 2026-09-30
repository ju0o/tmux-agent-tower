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


def resolve_adapter(command: str, title: str, cmdline: str = "") -> AgentAdapter:
    for adapter in ADAPTERS:
        if adapter.matches(command, title, cmdline):
            return adapter
    return ShellAdapter.UNKNOWN_FALLBACK


__all__ = [
    "AgentAdapter",
    "AdapterResult",
    "PaneContext",
    "ADAPTERS",
    "resolve_adapter",
]
