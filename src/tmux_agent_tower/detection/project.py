"""Project auto-discovery from a pane's current working directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional

_git_root_cache: Dict[str, Optional[str]] = {}


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
