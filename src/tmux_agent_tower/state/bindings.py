"""Project binding written when the Workspace Launcher creates a pane.

The pane id is reused after a tmux server restart, so a binding is used
only when the session and the pane's process id still match the ones
recorded at creation. A path that no longer exists is not shown: an
honest "(no name)" is safer than a confident wrong project.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from pathlib import Path
from typing import Dict, Optional

_PANE_ID = re.compile(r"^%\d+$")
_PID = re.compile(r"^\d+$")
_SESSION_ID = re.compile(r"^\$\d+$")
_PROVIDER_TYPES = {"local_tmux", "ssh_visible_history", "remote_tower", "remote_tmux", "agent_specific"}


class ProjectBindingStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[Dict[str, Dict[str, str]]] = None
        self._loaded_stamp: Optional[tuple] = None

    def _stamp(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        except Exception:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load(self) -> Dict[str, Dict[str, str]]:
        stamp = self._stamp()
        if self._cache is not None and stamp == self._loaded_stamp:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except Exception:
            data = {}
        cleaned: Dict[str, Dict[str, str]] = {}
        if isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                entry = {
                    field: str(value[field])
                    for field in (
                        "project_path",
                        "project_name",
                        "agent",
                        "session",
                        "pane_pid",
                        "created_at",
                        "execution_host",
                        "transport",
                        "transport_target",
                        "tmux_host",
                        "target_id",
                        "tower_pane_id",
                        "tower_pane_pid",
                        "tower_session",
                        "remote_session_identity",
                        "result_provider_type",
                        "result_provider_endpoint",
                        "result_provider_session_id",
                        "result_provider_pane_id",
                        "result_provider_pane_pid",
                        "result_provider_liveness",
                    )
                    if field in value and isinstance(value[field], (str, int, float))
                }
                if entry.get("project_path") and entry.get("pane_pid") and entry.get("session"):
                    cleaned[str(key)] = entry
        self._cache = cleaned
        self._loaded_stamp = stamp
        return self._cache

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass
        self._loaded_stamp = self._stamp()

    def record(
        self,
        key: str,
        session: str,
        pane_pid: str,
        project_path: str,
        project_name: str,
        agent: str,
        execution_host: str = "",
        transport: str = "",
        transport_target: str = "",
        tmux_host: str = "",
        result_provider: Optional[Dict[str, str]] = None,
    ) -> None:
        """Remember which project this new pane was opened for.

        A missing pid or session is not recorded: it could not be checked
        against a later pane that reused the same id.
        """

        if not key or not session or not str(pane_pid).strip() or not project_path:
            return
        data = self._load()
        entry = {
            "project_path": str(project_path),
            "project_name": str(project_name or ""),
            "agent": str(agent or ""),
            "session": str(session),
            "pane_pid": str(pane_pid).strip(),
            "created_at": str(int(time.time())),
            "tower_session": str(session),
            "tower_pane_id": str(key),
            "tower_pane_pid": str(pane_pid).strip(),
            "target_id": hashlib.sha256(
                "\0".join((tmux_host or "", str(session), str(key), str(pane_pid).strip())).encode()
            ).hexdigest(),
        }
        if execution_host:
            entry["execution_host"] = str(execution_host)
        if transport:
            entry["transport"] = str(transport)
        if transport_target:
            entry["transport_target"] = str(transport_target)
        if tmux_host:
            entry["tmux_host"] = str(tmux_host)
        if result_provider and self._valid_provider(result_provider):
            entry.update({name: str(value) for name, value in result_provider.items()})
            entry["remote_session_identity"] = str(result_provider.get("result_provider_session_id") or "")
        data[str(key)] = entry
        self._cache = data
        self._save()

    def rebind_project(
        self,
        key: str,
        session: str,
        pane_pid: str,
        project_path: str,
        project_name: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Change only this live pane's project binding, preserving providers."""

        if not key or not session or not str(pane_pid).strip() or not project_path or not project_name:
            return False
        data = self._load()
        entry = dict(data.get(str(key), {}))
        if entry and (
            str(entry.get("session") or "") != str(session)
            or str(entry.get("pane_pid") or "").strip() != str(pane_pid).strip()
        ):
            return False
        if not entry:
            meta = metadata or {}
            tmux_host = str(meta.get("tmux_host") or "")
            entry.update({
                "session": str(session),
                "pane_pid": str(pane_pid).strip(),
                "created_at": str(int(time.time())),
                "target_id": hashlib.sha256(
                    "\0".join((tmux_host, str(session), str(key), str(pane_pid).strip())).encode()
                ).hexdigest(),
            })
        entry["project_path"] = str(project_path)
        entry["project_name"] = str(project_name)
        for field in ("agent", "execution_host", "transport", "transport_target", "tmux_host"):
            value = str((metadata or {}).get(field) or "")
            if value:
                entry[field] = value
        data[str(key)] = entry
        self._cache = data
        self._save()
        return True

    def usable(self, key: str, session: str, pane_pid: str) -> Optional[Dict[str, str]]:
        """The binding for this exact pane, or None when it does not fit.

        Mismatch, a missing pid, or a project path that is gone are all
        treated as stale.
        """

        record = self._load().get(str(key))
        if not record:
            return None
        if str(record.get("session") or "") != str(session or ""):
            return None
        stored_pid = str(record.get("pane_pid") or "")
        if not stored_pid or stored_pid != str(pane_pid or "").strip():
            return None
        path = record.get("project_path") or ""
        remote_work = str(record.get("transport") or "") == "ssh"
        if remote_work:
            if not path and not (record.get("execution_host") or record.get("transport_target")):
                return None
            return record
        if not path or not Path(path).is_dir():
            return None
        return record

    @staticmethod
    def _valid_provider(provider: Dict[str, str]) -> bool:
        kind = str(provider.get("result_provider_type") or "")
        if kind not in _PROVIDER_TYPES:
            return False
        if kind in {"remote_tmux", "remote_tower"}:
            return bool(
                provider.get("result_provider_endpoint")
                and _SESSION_ID.fullmatch(str(provider.get("result_provider_session_id") or ""))
                and _PANE_ID.fullmatch(str(provider.get("result_provider_pane_id") or ""))
                and _PID.fullmatch(str(provider.get("result_provider_pane_pid") or ""))
            )
        return True

    def bind_result_provider(
        self,
        key: str,
        session: str,
        pane_pid: str,
        provider: Dict[str, str],
    ) -> bool:
        """Attach a provider only to the exact live Tower pane identity."""
        if not self._valid_provider(provider):
            return False
        record = self._load().get(str(key))
        if not record or record.get("session") != str(session) or record.get("pane_pid") != str(pane_pid):
            return False
        record.update({name: str(value) for name, value in provider.items()})
        record["result_provider_liveness"] = "registered"
        record["remote_session_identity"] = str(provider.get("result_provider_session_id") or "")
        self._save()
        return True
