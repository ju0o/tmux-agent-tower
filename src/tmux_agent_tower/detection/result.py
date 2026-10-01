"""In-memory final-answer state for one Tower Remote process.

NONE / READY / READ are not statuses. A quiet pane can be IDLE with
NONE. READY means a new final body was extracted. READ means the phone
opened or copied that body. The text itself is never written to disk.
Restarting Tower Remote forgets it on purpose.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from ..adapters.base import ResultCandidate

RESULT_NONE = "none"
RESULT_READY = "ready"
RESULT_READ = "read"

_SUPPRESSING = frozenset({"WORKING", "WAITING", "DEAD", "UNKNOWN"})


@dataclass
class ResultSnapshot:
    state: str
    text: str
    fingerprint: str
    generated_at: float = 0.0


@dataclass
class _Rec:
    fingerprint: str = ""
    text: str = ""
    state: str = RESULT_NONE
    suppressed: bool = False
    generated_at: float = 0.0


class ResultTracker:
    def __init__(self) -> None:
        self._recs: Dict[str, _Rec] = {}

    def observe(self, pane_key: str, status: str, candidate: Optional[ResultCandidate]) -> ResultSnapshot:
        rec = self._recs.setdefault(pane_key, _Rec())

        if status in _SUPPRESSING:
            # A new turn (or an unreadable screen) must not announce the
            # previous answer again. The fingerprint stays so the same
            # body cannot become READY a second time when it is still
            # sitting in scrollback.
            rec.suppressed = True
            return ResultSnapshot(RESULT_NONE, "", rec.fingerprint, rec.generated_at)

        if candidate is None:
            if rec.suppressed:
                return ResultSnapshot(RESULT_NONE, "", rec.fingerprint, rec.generated_at)
            if rec.state in (RESULT_READY, RESULT_READ) and rec.text:
                return ResultSnapshot(rec.state, rec.text, rec.fingerprint, rec.generated_at)
            return ResultSnapshot(RESULT_NONE, "", rec.fingerprint, rec.generated_at)

        if candidate.fingerprint != rec.fingerprint:
            rec.fingerprint = candidate.fingerprint
            rec.text = candidate.text
            rec.state = RESULT_READY
            rec.suppressed = False
            rec.generated_at = time.time()
            return ResultSnapshot(RESULT_READY, rec.text, rec.fingerprint, rec.generated_at)

        if rec.suppressed:
            rec.suppressed = False
            if rec.state != RESULT_READ:
                rec.state = RESULT_NONE
                rec.text = ""
            return ResultSnapshot(rec.state, rec.text, rec.fingerprint, rec.generated_at)

        return ResultSnapshot(rec.state, rec.text, rec.fingerprint, rec.generated_at)

    def snapshot(self, pane_key: str) -> ResultSnapshot:
        rec = self._recs.get(pane_key)
        if rec is None or rec.suppressed or rec.state == RESULT_NONE:
            fingerprint = rec.fingerprint if rec else ""
            generated = rec.generated_at if rec else 0.0
            return ResultSnapshot(RESULT_NONE, "", fingerprint, generated)
        return ResultSnapshot(rec.state, rec.text, rec.fingerprint, rec.generated_at)

    def mark_read(self, pane_key: str, fingerprint: str) -> bool:
        rec = self._recs.get(pane_key)
        if rec is None or not fingerprint or rec.fingerprint != fingerprint:
            return False
        if rec.state != RESULT_READY:
            return rec.state == RESULT_READ
        rec.state = RESULT_READ
        return True
