"""Plain shell / SSH fallback adapter.

This is also used as the last-resort adapter when no other adapter's
``command_names``/``title_hints`` match (see ``ADAPTERS.resolve_adapter``),
so it deliberately never raises and never assumes an unrecognised program is
idle just because the shell prompt regex fails to match some exotic PS1.
"""

from __future__ import annotations

from .base import AgentAdapter, AdapterResult, PaneContext, SHELL_PROMPT_RE, looks_like_generic_waiting


class ShellAdapter(AgentAdapter):
    name = "Shell"
    command_names = ("bash", "zsh", "fish", "sh", "ssh")
    title_hints = ()

    def classify(self, ctx: PaneContext) -> AdapterResult:
        tail = ctx.tail(20)

        if looks_like_generic_waiting(tail):
            return AdapterResult("WAITING", "approval-prompt")

        last = ctx.last_nonblank()
        if last and SHELL_PROMPT_RE.search(last):
            return AdapterResult("IDLE", "shell-prompt")

        return AdapterResult(None)


# Used by adapters/__init__.py as the final fallback for genuinely
# unrecognised commands (neither a known agent nor a known shell). It
# behaves identically to ShellAdapter's classify() but is never matched by
# name, so its "Agent" label in the UI stays honest (falls back to the raw
# command string, not "Shell").
ShellAdapter.UNKNOWN_FALLBACK = ShellAdapter()
