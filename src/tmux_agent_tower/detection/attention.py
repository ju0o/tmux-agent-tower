"""Attention is not an execution status.

Execution, attention, and result stay on separate axes. A pane can be
IDLE, waiting for approval, and holding a READY result at the same time.
"""

from __future__ import annotations

ATTENTION_NONE = "none"
ATTENTION_APPROVAL = "approval_required"
ATTENTION_INPUT = "input_required"
ATTENTION_ERROR = "error"

MAX_ATTENTION_PROMPT = 160


def clamp_prompt(text: str) -> str:
    """One short question, not a transcript."""

    flat = " ".join((text or "").split())
    if len(flat) <= MAX_ATTENTION_PROMPT:
        return flat
    return flat[: MAX_ATTENTION_PROMPT - 1] + "…"


def card_rank(row: dict) -> int:
    """Phone and the TUI attention view share this order.

    Legacy ``status == WAITING`` sorts with input, so older rows still
    rise. The three axes themselves are not merged into one status.
    """

    attention = row.get("attention") or ATTENTION_NONE
    if attention == ATTENTION_APPROVAL:
        return 0
    if attention == ATTENTION_INPUT or row.get("status") == "WAITING":
        return 1
    if row.get("result_state") == "ready":
        return 2
    # Error and an unreadable screen both need a look before ordinary work.
    if attention == ATTENTION_ERROR or row.get("status") == "UNKNOWN":
        return 3
    return {"WORKING": 4, "IDLE": 5, "DEAD": 6}.get(row.get("status") or "", 7)
