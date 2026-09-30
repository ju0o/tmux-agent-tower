"""Project auto-discovery from a pane's current working directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional

_git_root_cache: Dict[str, Optional[str]] = {}

# Directory basenames that carry no real project information -- a mount
# point, a home directory, a drive letter under WSL (`/mnt/f` -> "f"), and
# similar. Showing these as *the* project name is actively misleading, so
# the display-priority chain in ui/render.py treats them as low-confidence
# and prefers a pane title or an honest "(no name)" instead.
_GENERIC_BASENAMES = {
    "mnt", "home", "root", "tmp", "usr", "var", "srv", "media", "run",
    "etc", "opt", "bin", "sbin", "lib", "proc", "sys", "dev", "users",
    "documents", "desktop", "downloads",
}


def _git_toplevel(path: str, timeout: float = 1.5) -> Optional[str]:
    if path in _git_root_cache:
        return _git_root_cache[path]

    root: Optional[str] = None
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            candidate = result.stdout.strip()
            if candidate:
                root = candidate
    except Exception:
        root = None

    _git_root_cache[path] = root
    return root


def discover_project(path: str) -> str:
    """Best-effort project name for a pane's current directory.

    Prefers the basename of the enclosing git repository's root; falls back
    to the basename of the pane's current path; falls back to the raw path
    if even that is empty (e.g. ``/``).
    """

    if not path:
        return "(unknown)"

    root = _git_toplevel(path)
    basename = Path(root or path).name

    return basename or path


def git_project_name(path: str) -> Optional[str]:
    """The git repo's basename if ``path`` is inside one, else ``None``.

    Separate from ``discover_project`` so callers building a fallback
    priority chain (custom > git name > pane title > basename) can tell
    "this came from an actual git repo" apart from "this is just the raw
    directory basename" -- ``discover_project`` alone conflates the two.
    """

    if not path:
        return None

    root = _git_toplevel(path)
    if not root:
        return None

    return Path(root).name or None


def is_low_confidence_name(name: Optional[str]) -> bool:
    """True for a directory-basename-shaped name that carries little or no
    real project information: empty, a single character (a WSL drive
    letter like "f"), or a generic mount/home-ish directory name.
    """

    if not name:
        return True

    stripped = name.strip()

    if len(stripped) <= 1:
        return True

    if stripped.lower() in _GENERIC_BASENAMES:
        return True

    return False
