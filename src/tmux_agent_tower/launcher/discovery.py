"""Project discovery + recent-projects tracking for the Workspace Launcher.

Read-only: walks the filesystem (or runs a single read-only ``find`` over
SSH for a remote host) looking for ``.git`` directories. Never executes
anything else, never modifies a discovered project.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

_SKIP_DIR_NAMES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".cache",
    ".tox", "dist", "build", ".next", ".turbo",
}

MAX_RECENT = 20
_SAFE_HOST_RE = re.compile(r"[^A-Za-z0-9_-]")

# Mirrors config._CANDIDATE_ROOT_NAMES -- kept local to avoid a launcher ->
# launcher submodule import cycle for one shared list.
_CANDIDATE_ROOT_NAMES = ["Projects", "projects", "code", "Code", "dev", "src", "workspace", "Workspace"]


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


def _recent_file(state_dir: Path, host_key: str) -> Path:
    safe_host = _SAFE_HOST_RE.sub("_", host_key)
    return state_dir / f"recent-projects.{safe_host}.json"


def load_recent(state_dir: Path, host_key: str) -> List[str]:
    """Recent projects are scoped per host: a MAINPC path recorded here
    must never bleed into ASUS's list (or vice versa), since a path from
    one host's filesystem is almost certainly meaningless on the other.
    """

    path = _recent_file(state_dir, host_key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(p) for p in data]
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return []


def record_recent(state_dir: Path, host_key: str, project_path: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    recent = [p for p in load_recent(state_dir, host_key) if p != project_path]
    recent.insert(0, project_path)
    recent = recent[:MAX_RECENT]

    path = _recent_file(state_dir, host_key)
    try:
        path.write_text(json.dumps(recent, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_remote_git_projects(
    host_alias: str, max_depth: int = 3, limit: int = 200, timeout: float = 6.0
) -> List[ProjectEntry]:
    """Best-effort remote equivalent of ``find_git_projects``.

    One read-only SSH round trip: probe which of the usual candidate
    directory names exist under the remote ``$HOME``, then run a single
    bounded ``find`` for ``.git`` directories under those. Times out
    rather than hanging the wizard; any failure degrades to an empty list
    so the caller can fall back to manual path entry.
    """

    root_probe = " ; ".join(f'[ -d "$HOME/{name}" ] && echo "$HOME/{name}"' for name in _CANDIDATE_ROOT_NAMES)
    script = (
        "set -u\n"
        f"ROOTS=$({{ {root_probe} ; }} 2>/dev/null)\n"
        'if [ -z "$ROOTS" ]; then exit 0; fi\n'
        f'find $ROOTS -maxdepth {max_depth} -type d -name .git 2>/dev/null | head -n {limit}\n'
    )

    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}", host_alias, script],
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
            check=False,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    entries: List[ProjectEntry] = []
    for line in (result.stdout or "").splitlines():
        git_dir = line.strip()
        if not git_dir.endswith("/.git"):
            continue
        project_path = git_dir[: -len("/.git")]
        name = project_path.rsplit("/", 1)[-1]
        entries.append(ProjectEntry(name=name, path=project_path, is_git=True))

    entries.sort(key=lambda p: p.name.lower())
    return entries
