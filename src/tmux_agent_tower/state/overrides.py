"""Custom project/agent/pane-title overrides (the "E" edit menu).

Stored separately from auto-detection so that:

* auto-discovery (git-root basename, adapter-detected agent name) never
  clobbers a value the user explicitly chose, on this or any future
  refresh, and
* a manual agent-name override is display-only metadata -- it never
  changes what process is running or sends it anything (see
  ``ui/launcher_wizard.py``'s module docstring for the same boundary
  applied to spawning; this is the same boundary applied to labeling).

Keyed by pane_id (or a ``"alias:pane_id"`` composite for remote panes).
Note tmux pane ids (``%12``) are stable for the lifetime of the tmux
server but are reused after a server restart, so an override can in rare
cases "stick" to an unrelated later pane that reused the same id. This is
a known, documented limitation (see README) rather than a correctness bug
worth a heavier keying scheme for a v0.1.x prototype.

v0.1.2 note: the on-disk format changed from ``{key: "project name"}`` to
``{key: {"project": ..., "agent": ..., "title": ...}}``. ``_load()``
migrates the old flat-string shape in place (in memory; it's rewritten to
the new shape on the next write) so existing overrides are never lost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

FIELDS = ("project", "agent", "title")


class OverrideStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[Dict[str, Dict[str, str]]] = None

    def _load(self) -> Dict[str, Dict[str, str]]:
        if self._cache is not None:
            return self._cache

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except Exception:
            # Malformed override file: fail safe to "no overrides" rather
            # than crashing the whole TUI.
            data = {}

        migrated: Dict[str, Dict[str, str]] = {}

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    # v0.1.0/v0.1.1 shape: a bare project-name string.
                    migrated[str(key)] = {"project": value}
                elif isinstance(value, dict):
                    entry = {
                        field: str(value[field])
                        for field in FIELDS
                        if field in value and isinstance(value[field], (str, int, float))
                    }
                    if entry:
                        migrated[str(key)] = {k: str(v) for k, v in entry.items()}

        self._cache = migrated
        return self._cache

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass

    def _get_field(self, key: str, field: str) -> Optional[str]:
        return self._load().get(key, {}).get(field)

    def _set_field(self, key: str, field: str, value: str) -> None:
        data = self._load()
        entry = dict(data.get(key, {}))
        entry[field] = value
        data[key] = entry
        self._cache = data
        self._save()

    def get_project(self, key: str) -> Optional[str]:
        return self._get_field(key, "project")

    def set_project(self, key: str, value: str) -> None:
        self._set_field(key, "project", value)

    def get_agent(self, key: str) -> Optional[str]:
        return self._get_field(key, "agent")

    def set_agent(self, key: str, value: str) -> None:
        self._set_field(key, "agent", value)

    def get_title(self, key: str) -> Optional[str]:
        return self._get_field(key, "title")

    def set_title(self, key: str, value: str) -> None:
        self._set_field(key, "title", value)

    def clear_field(self, key: str, field: str) -> None:
        """Clears just one field (e.g. "use auto-detected agent again")
        without touching any other override for the same pane -- unlike
        ``reset()``, which clears all of them.
        """

        data = self._load()
        entry = data.get(key)
        if not entry or field not in entry:
            return

        entry = dict(entry)
        del entry[field]

        if entry:
            data[key] = entry
        else:
            del data[key]

        self._cache = data
        self._save()

    def reset(self, key: str) -> None:
        """Clears every override (project/agent/title) for ``key`` --
        only this one pane, never the whole store.
        """

        data = self._load()
        if key in data:
            del data[key]
            self._cache = data
            self._save()
