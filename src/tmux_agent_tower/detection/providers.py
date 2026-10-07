"""Explicit mapping from a Tower pane to the source allowed to provide Result."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from typing import Mapping, Optional, Protocol

from ..adapters.base import ResultCandidate


@dataclass(frozen=True)
class ResultTarget:
    target_id: str
    tower_pane_id: str
    tower_pane_pid: str
    execution_host: str
    transport: str
    remote_session_identity: str
    provider_type: str
    provider_endpoint: str
    provider_session_id: str
    provider_pane_id: str
    provider_pid: str
    provider_liveness: str
    agent: str = ""
    command: str = ""
    title: str = ""
    cmdline: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "ResultTarget":
        pane = str(row.get("pane_id") or row.get("key") or "")
        pane_pid = str(row.get("pane_pid") or "")
        tower_pane = str(row.get("tower_pane_id") or ("" if row.get("remote") else pane))
        tower_pid = str(row.get("tower_pane_pid") or ("" if row.get("remote") else pane_pid))
        provider_type = str(row.get("result_provider_type") or "")
        provider_pane = str(row.get("result_provider_pane_id") or (pane if row.get("remote") else ""))
        provider_pid = str(row.get("result_provider_pane_pid") or (pane_pid if row.get("remote") else ""))
        provider_session = str(row.get("result_provider_session_id") or (row.get("session_id") or "" if row.get("remote") else ""))
        endpoint = str(row.get("result_provider_endpoint") or row.get("result_provider_host") or "")
        physical = "\0".join((
            str(row.get("tmux_host") or row.get("host") or ""),
            str(row.get("session_id") or row.get("session") or ""), pane, pane_pid,
        ))
        target_id = str(row.get("target_id") or hashlib.sha256(physical.encode("utf-8", "replace")).hexdigest())
        return cls(
            target_id=target_id,
            tower_pane_id=tower_pane,
            tower_pane_pid=tower_pid,
            execution_host=str(row.get("execution_host") or row.get("host") or "UNKNOWN"),
            transport=str(row.get("transport") or "unknown"),
            remote_session_identity=str(row.get("remote_session_identity") or provider_session),
            provider_type=provider_type,
            provider_endpoint=endpoint,
            provider_session_id=provider_session,
            provider_pane_id=provider_pane,
            provider_pid=provider_pid,
            provider_liveness=str(row.get("result_provider_liveness") or "unknown"),
            agent=str(row.get("agent") or row.get("auto_agent") or ""),
            command=str(row.get("command") or ""),
            title=str(row.get("pane_title") or ""),
            cmdline=str(row.get("cmdline") or ""),
        )

    def remote_provider_row(self) -> dict:
        return {
            "result_provider_type": self.provider_type,
            "result_provider_endpoint": self.provider_endpoint,
            "result_provider_session_id": self.provider_session_id,
            "result_provider_pane_id": self.provider_pane_id,
            "result_provider_pane_pid": self.provider_pid,
            "result_provider_liveness": self.provider_liveness,
            "pane_title": self.title,
            "command": self.command,
            "cmdline": self.cmdline,
        }


class ResultProvider(Protocol):
    def get_latest_complete_result(self, target: ResultTarget) -> Optional[ResultCandidate]:
        """Return one complete newest turn from this explicitly mapped source."""


def normalize_candidate(
    candidate: ResultCandidate,
    target: ResultTarget,
    *,
    source: str,
    timestamp: Optional[float] = None,
) -> ResultCandidate:
    if not candidate.text or not candidate.complete:
        return replace(candidate, source=source, timestamp=timestamp or time.time())
    turn = "\0".join((target.target_id, target.provider_type or source, candidate.fingerprint))
    return replace(
        candidate,
        source=source,
        turn_identity=candidate.turn_identity or hashlib.sha256(turn.encode("utf-8", "replace")).hexdigest(),
        timestamp=time.time() if timestamp is None else timestamp,
    )
