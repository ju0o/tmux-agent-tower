"""Status engine v2.

Design constraints (see docs/STATUS_ENGINE.md for the full rationale):

* STATUS and VISIT are completely independent. Whether a human has looked
  at a pane yet must never change what status is reported for it. This
  engine has no notion of "seen"/"new" at all.
* Execution states: WORKING, IDLE, UNKNOWN, DEAD. Attention (approval,
  input, error) and result are separate axes and are not returned here.
* A wrong WORKING or wrong IDLE is worse than an honest UNKNOWN.
* Hysteresis: a WORKING verdict is "held" for a short window after the
  screen last changed, so a pane that pauses output for a second or two
  (thinking, waiting on a slow tool call) doesn't flicker back to IDLE and
  then to WORKING again on the next refresh.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence

from ..adapters.base import AgentAdapter, PaneContext

STATUS_WORKING = "WORKING"
STATUS_WAITING = "WAITING"
STATUS_IDLE = "IDLE"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_DEAD = "DEAD"

DEFAULT_WORKING_HOLD_SECONDS = 8.0


def _hash_lines(lines: Sequence[str]) -> str:
    joined = "\n".join(lines)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class _PaneState:
    last_hash: Optional[str] = None
    active_until: float = 0.0
    observations: int = 0
    current_status: Optional[str] = None
    status_since: float = 0.0
    runtime_identity: Optional[str] = None


class StatusEngine:
    """Stateful evaluator: call ``evaluate`` once per pane per refresh tick."""

    def __init__(self, hold_seconds: float = DEFAULT_WORKING_HOLD_SECONDS):
        self.hold_seconds = hold_seconds
        self._state: Dict[str, _PaneState] = {}

    def forget(self, pane_id: str) -> None:
        self._state.pop(pane_id, None)

    def duration_seconds(self, pane_id: str, now: Optional[float] = None) -> float:
        """How long ``pane_id`` has continuously reported its current
        status, per this engine's own observations -- NOT "when did the
        agent actually start," which we have no way to know. Resets to 0
        whenever the reported status changes, and whenever Tower itself
        restarts (this is in-memory only, deliberately not persisted --
        see docs/STATUS_ENGINE.md).
        """

        state = self._state.get(pane_id)
        if state is None or state.current_status is None:
            return 0.0
        now = now if now is not None else time.monotonic()
        return max(0.0, now - state.status_since)

    def evaluate(
        self,
        pane_id: str,
        dead: bool,
        adapter: AgentAdapter,
        ctx: PaneContext,
        now: Optional[float] = None,
        runtime_identity: Optional[str] = None,
    ) -> str:
        now = now if now is not None else time.monotonic()
        status = self._evaluate_status(pane_id, dead, adapter, ctx, now, runtime_identity)

        # Deliberately NOT calling forget() for a dead pane (an earlier
        # version did, every single observation) -- that reset
        # status_since to "now" on every refresh, so a DEAD pane's
        # duration could never accumulate past one refresh interval.
        # Tracking is instead reset lazily, only on the transition BACK
        # from dead to alive (see _evaluate_status), when a stale
        # hash/observation baseline would actually be wrong (pane_id
        # reused by a genuinely new process).
        state = self._state.setdefault(pane_id, _PaneState())

        if state.current_status != status:
            state.current_status = status
            state.status_since = now

        return status

    def _evaluate_status(
        self,
        pane_id: str,
        dead: bool,
        adapter: AgentAdapter,
        ctx: PaneContext,
        now: float,
        runtime_identity: Optional[str],
    ) -> str:
        if dead:
            return STATUS_DEAD

        state = self._state.setdefault(pane_id, _PaneState())

        # pane_id is a display address, not a runtime identity. tmux can
        # respawn a pane under the same id; never carry its old hash or
        # working hold into a different (or no longer identifiable) process.
        if state.runtime_identity != runtime_identity:
            state = _PaneState(runtime_identity=runtime_identity)
            self._state[pane_id] = state

        if state.current_status == STATUS_DEAD:
            # Coming back from dead: pane_id was reused by a new process,
            # so the old hash/observation baseline no longer means anything.
            state.last_hash = None
            state.active_until = 0.0
            state.observations = 0

        digest = _hash_lines(ctx.lines)
        changed = state.last_hash is not None and digest != state.last_hash
        first_observation = state.observations == 0

        state.last_hash = digest
        state.observations += 1

        opinion = adapter.classify(ctx)

        # A recognized current question or approval prompt proves this turn
        # is waiting for the user. Keep that execution fact separate from
        # the attention value returned by the adapter.
        if adapter.detect_attention(ctx) in ("approval_required", "input_required"):
            state.active_until = 0.0
            return STATUS_IDLE

        # 1. Strong, agent-specific WORKING evidence always wins and refreshes
        #    the hold window.
        if opinion.status == STATUS_WORKING:
            state.active_until = now + self.hold_seconds
            return STATUS_WORKING

        # 2. An idle widget is the current frame. A ticking clock or cursor
        #    under that widget is not work, and it must not keep the hold.
        if opinion.status == STATUS_IDLE:
            state.active_until = 0.0
            return STATUS_IDLE

        # 3. The screen is actually producing new output right now.
        if changed:
            state.active_until = now + self.hold_seconds
            return STATUS_WORKING

        # 4. Output paused very recently after being active -- hold WORKING
        #    briefly instead of flapping to IDLE/UNKNOWN and back.
        if state.active_until > now:
            return STATUS_WORKING

        # 5. No adapter opinion and no recent output.
        blank = not ctx.lines or all(not line.strip() for line in ctx.lines)

        if first_observation or blank:
            # We have not established a stable baseline for this pane yet
            # (or there is nothing on screen to reason about) -- an honest
            # "don't know" beats guessing IDLE or WORKING.
            return STATUS_UNKNOWN

        # 6. Stable and non-blank, but the adapter has no opinion. IDLE
        #    here was a guess: a paused tool call looked idle. UNKNOWN
        #    is the honest execution state. Attention stays separate.
        return STATUS_UNKNOWN
