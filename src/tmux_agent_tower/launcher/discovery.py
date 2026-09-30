"""Project discovery + recent-projects tracking for the Workspace Launcher.

Read-only: walks the filesystem looking for ``.git`` directories. Never
executes anything, never modifies a discovered project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

_SKIP_DIR_NAMES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
    ".tox", "dist", "build", ".next", ".turbo",
}

RECENT_FILE_NAME = "recent-projects.json"
MAX_RECENT = 20


@dataclass(frozen=True)
class ProjectEntry:
    name: str
    path: str
    is_git: bool = True


def find_git_projects(roots: List[str], max_depth: int = 3, limit: int = 500) -> List[ProjectEntry]:
    seen_paths = set()
    results: List[ProjectEntry] = []

    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            continue

        _walk(root_path, max_depth, seen_paths, results, limit)

        if len(results) >= limit:
            break

    results.sort(key=lambda p: p.name.lower())
    return results


def _walk(directory: Path, depth_remaining: int, seen_paths: set, results: list, limit: int) -> None:
    if len(results) >= limit:
        return

    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except (PermissionError, OSError):
        return

    if (directory / ".git").exists():
        resolved = str(directory.resolve())
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            results.append(ProjectEntry(name=directory.name, path=resolved, is_git=True))
        # Don't descend into an already-found repo's internals.
        return

    if depth_remaining <= 0:
        return

    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".") or entry.name in _SKIP_DIR_NAMES:
            continue
        _walk(entry, depth_remaining - 1, seen_paths, results, limit)
        if len(results) >= limit:
            return


def manual_path_entry(path: str) -> ProjectEntry:
    resolved = Path(path).expanduser()
    is_git = (resolved / ".git").exists()
    return ProjectEntry(name=resolved.name or str(resolved), path=str(resolved), is_git=is_git)


def load_recent(state_dir: Path) -> List[str]:
    path = state_dir / RECENT_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(p) for p in data]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return []


def record_recent(state_dir: Path, project_path: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    recent = [p for p in load_recent(state_dir) if p != project_path]
    recent.insert(0, project_path)
    recent = recent[:MAX_RECENT]

    path = state_dir / RECENT_FILE_NAME
    try:
        path.write_text(json.dumps(recent, indent=2), encoding="utf-8")
    except Exception:
        pass
