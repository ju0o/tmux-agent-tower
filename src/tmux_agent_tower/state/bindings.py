"""Project binding written when the Workspace Launcher creates a pane.

The pane id is reused after a tmux server restart, so a binding is used
only when the session and the pane's process id still match the ones
recorded at creation. A path that no longer exists is not shown: an
honest "(no name)" is safer than a confident wrong project.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, Optional


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
                    for field in ("project_path", "project_name", "agent", "session", "pane_pid", "created_at")
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
    ) -> None:
        """Remember which project this new pane was opened for.

        A missing pid or session is not recorded: it could not be checked
        against a later pane that reused the same id.
        """

        if not key or not session or not str(pane_pid).strip() or not project_path:
            return
        data = self._load()
        data[str(key)] = {
            "project_path": str(project_path),
            "project_name": str(project_name or ""),
            "agent": str(agent or ""),
            "session": str(session),
            "pane_pid": str(pane_pid).strip(),
            "created_at": str(int(time.time())),
        }
        self._cache = data
        self._save()

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
        if not path or not Path(path).is_dir():
            return None
        return record
