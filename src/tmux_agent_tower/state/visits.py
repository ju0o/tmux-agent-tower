"""NEW / SEEN visit tracking, persisted per tmux session.

This is deliberately the *only* thing that "seen" affects. It must never
feed back into status detection (see detection/status.py's module
docstring) -- that was the bug in the original prototype, where an
unvisited-but-working pane was reported as a fake "CHECKING" status.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

VISIT_NEW = "NEW"
VISIT_SEEN = "SEEN"

# Dots are deliberately excluded (not just "/") so a session name can never
# produce a filename containing "..".
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


class VisitStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _seen_file(self, session: str) -> Path:
        safe = _SAFE_RE.sub("_", session)
        return self.state_dir / f"seen.{safe}"

    def _read(self, session: str) -> Set[str]:
        path = self._seen_file(session)
        try:
            return {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except FileNotFoundError:
            return set()
        except Exception:
            return set()

    def visit_label(self, session: str, pane_id: str) -> str:
        return VISIT_SEEN if pane_id in self._read(session) else VISIT_NEW

    def mark_seen(self, session: str, pane_id: str) -> None:
        seen = self._read(session)
        if pane_id in seen:
            return

        path = self._seen_file(session)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(pane_id + "\n")
        except Exception:
            pass
