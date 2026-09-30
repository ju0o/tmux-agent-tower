"""Custom project/title overrides (the "E" rename action).

Stored separately from tmux's own pane title so that:

* pressing "E" never rewrites the user's actual tmux pane title (some users
  script against pane titles for other tools), and
* auto-discovery (git-root basename) never clobbers a name the user
  explicitly chose, on this or any future refresh.

Keyed by pane_id. Note tmux pane ids (``%12``) are stable for the lifetime
of the tmux server but are reused after a server restart, so an override
can in rare cases "stick" to an unrelated later pane that reused the same
id. This is a known, documented limitation (see README) rather than a
correctness bug worth a heavier keying scheme for a v0.1.0 prototype.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional


class OverrideStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[Dict[str, str]] = None

    def _load(self) -> Dict[str, str]:
        if self._cache is not None:
            return self._cache

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._cache = {str(k): str(v) for k, v in data.items()}
            else:
                self._cache = {}
        except FileNotFoundError:
            self._cache = {}
        except Exception:
            # Malformed override file: fail safe to "no overrides" rather
            # than crashing the whole TUI.
            self._cache = {}

        return self._cache

    def get(self, pane_id: str) -> Optional[str]:
        return self._load().get(pane_id)

    def set(self, pane_id: str, title: str) -> None:
        data = self._load()
        data[pane_id] = title
        self._cache = data

        try:
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass
