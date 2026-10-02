"""Read-only workspace browsing for one host.

Local listings use ``iterdir`` on the directory the user opened. Remote
listings use one SSH command for that directory. Nothing here creates a
pane, starts a process, or writes a file. A missing path is reported as
typed; it is not replaced with a nearby guess.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..ui.render import display_width, use_narrow_layout
from .discovery import ProjectEntry, _CANDIDATE_ROOT_NAMES, _SKIP_DIR_NAMES

# Depth and result caps for search. The picker scrolls the whole list;
# these bounds stop a walk of the entire filesystem.
SEARCH_MAX_DEPTH = 4
SEARCH_LIMIT = 500
CACHE_LIMIT = 64

_SKIP = set(_SKIP_DIR_NAMES) | {".git"}


@dataclass(frozen=True)
class BrowseError:
    code: str
    path: str
    host: str = ""


@dataclass(frozen=True)
class PathCheck:
    ok: bool
    entry: Optional[ProjectEntry] = None
    error: Optional[BrowseError] = None


@dataclass(frozen=True)
class DirectoryPage:
    path: str
    is_git: bool = False
    children: Tuple[ProjectEntry, ...] = ()
    error: Optional[BrowseError] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class DirectoryCache:
    """Small success-only cache keyed by host and path.

    Failures are not stored, so a later attempt can reach the host again.
    """

    limit: int = CACHE_LIMIT
    _data: Dict[Tuple[str, str], DirectoryPage] = field(default_factory=dict)
    _order: List[Tuple[str, str]] = field(default_factory=list)

    def get(self, host: str, path: str) -> Optional[DirectoryPage]:
        return self._data.get((host, path))

    def put(self, host: str, path: str, page: DirectoryPage) -> None:
        if page.error is not None:
            return
        key = (host, path)
        if key in self._data:
            self._order.remove(key)
        self._order.append(key)
        self._data[key] = page
        while len(self._order) > self.limit:
            self._data.pop(self._order.pop(0), None)


def directory_name(path: str) -> str:
    name = Path(path).name
    return name or path or "/"


def parent_path(path: str) -> str:
    """Parent directory. ``/`` stays ``/``."""

    current = Path(path)
    parent = current.parent
    if parent == current:
        return str(current)
    return str(parent)


def kind_label(is_git: bool) -> str:
    return "Git" if is_git else "Folder"


def format_entry_lines(name: str, path: str, is_git: bool, width: int) -> List[str]:
    """Name and full path. A narrow terminal puts the path on the next line."""

    kind = kind_label(is_git)
    one = f"{name}  {path}  {kind}"
    if use_narrow_layout(width) or display_width(one) > max(8, width - 4):
        return [f"{name}  {kind}", path]
    return [one]


def entry_detail(path: str) -> str:
    return path


def filter_entries(entries: Sequence[ProjectEntry], query: str) -> List[ProjectEntry]:
    """Match name or path against the full list, not the rows on screen."""

    text = (query or "").strip().lower()
    if not text:
        return list(entries)
    return [entry for entry in entries if text in entry.name.lower() or text in entry.path.lower()]


def _entry_for(path: Path) -> ProjectEntry:
    git = (path / ".git").exists()
    return ProjectEntry(name=directory_name(str(path)), path=str(path), is_git=git)


def validate_local_path(raw: str) -> PathCheck:
    """Exist, be a directory, then show the canonical path.

    ``~`` is expanded. A path that does not exist is returned unchanged
    apart from that expansion. No other path is substituted.
    """

    text = (raw or "").strip()
    if not text:
        return PathCheck(False, error=BrowseError("empty", text))
    expanded = str(Path(text).expanduser())
    path = Path(expanded)
    try:
        exists = path.exists()
    except OSError:
        return PathCheck(False, error=BrowseError("denied", expanded))
    if not exists:
        return PathCheck(False, error=BrowseError("not_found", expanded))
    try:
        if not path.is_dir():
            return PathCheck(False, error=BrowseError("not_dir", expanded))
        canon = path.resolve()
    except OSError:
        return PathCheck(False, error=BrowseError("denied", expanded))
    if not os.access(canon, os.R_OK | os.X_OK):
        return PathCheck(False, error=BrowseError("denied", expanded))
    return PathCheck(True, entry=_entry_for(canon))


def list_local_children(path: str) -> DirectoryPage:
    """Direct child directories only. Does not walk grandchildren."""

    check = validate_local_path(path)
    if not check.ok or check.entry is None or check.error is not None:
        error = check.error or BrowseError("not_found", path)
        return DirectoryPage(path=error.path, error=error)
    directory = Path(check.entry.path)
    try:
        names = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    except (PermissionError, OSError):
        return DirectoryPage(path=str(directory), is_git=check.entry.is_git, error=BrowseError("denied", str(directory)))

    children: List[ProjectEntry] = []
    for entry in names:
        if entry.name.startswith(".") or entry.name in _SKIP:
            continue
        try:
            if not entry.is_dir():
                continue
            resolved = entry.resolve()
            if not resolved.is_dir():
                continue
            if not os.access(resolved, os.R_OK | os.X_OK):
                continue
        except OSError:
            continue
        children.append(_entry_for(resolved))
    return DirectoryPage(path=str(directory), is_git=check.entry.is_git, children=tuple(children))


def local_start_roots(
    project_roots: Sequence[str],
    recent: Sequence[str],
    *,
    home: Optional[str] = None,
    mnt: str = "/mnt",
) -> List[str]:
    """Start locations that exist: configured roots, recent, home, /mnt drives."""

    found: List[str] = []

    def add(raw: object) -> None:
        if not raw:
            return
        check = validate_local_path(str(raw))
        if not check.ok or check.entry is None:
            return
        if check.entry.path not in found:
            found.append(check.entry.path)

    for root in project_roots:
        add(root)
    for path in recent:
        add(path)
    add(home if home is not None else str(Path.home()))
    mount = Path(mnt)
    try:
        if mount.is_dir():
            for child in sorted(mount.iterdir(), key=lambda item: item.name.lower()):
                add(child)
    except OSError:
        pass
    return found


def collect_local_catalog(
    roots: Sequence[str],
    *,
    max_depth: int = SEARCH_MAX_DEPTH,
    limit: int = SEARCH_LIMIT,
) -> List[ProjectEntry]:
    """Git repos and other directories inside ``roots``, depth-capped.

    A git repo is not descended into. Directories outside ``roots`` are
    not visited.
    """

    git: List[ProjectEntry] = []
    folders: List[ProjectEntry] = []
    seen = set()

    def children_of(directory: Path) -> List[Path]:
        try:
            found = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except (PermissionError, OSError):
            return []
        usable = []
        for child in found:
            if child.name.startswith(".") or child.name in _SKIP:
                continue
            try:
                if not child.is_dir() or child.is_symlink():
                    continue
            except OSError:
                continue
            usable.append(child)
        return usable

    def walk_git(directory: Path, depth: int) -> None:
        if len(git) >= limit:
            return
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if (resolved / ".git").exists():
            if str(resolved) not in seen:
                seen.add(str(resolved))
                git.append(_entry_for(resolved))
            return
        if depth <= 0:
            return
        for child in children_of(resolved):
            walk_git(child, depth - 1)

    def walk_folders(directory: Path, depth: int) -> None:
        if len(git) + len(folders) >= limit:
            return
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if (resolved / ".git").exists():
            return
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            folders.append(_entry_for(resolved))
        if depth <= 0:
            return
        for child in children_of(resolved):
            walk_folders(child, depth - 1)

    resolved_roots: List[Path] = []
    for root in roots:
        check = validate_local_path(str(root))
        if check.ok and check.entry is not None:
            resolved_roots.append(Path(check.entry.path))
    for root in resolved_roots:
        walk_git(root, max_depth)
    for root in resolved_roots:
        walk_folders(root, max_depth)

    git.sort(key=lambda entry: entry.name.lower())
    folders.sort(key=lambda entry: entry.name.lower())
    return git + folders


def script_is_read_only(script: str) -> bool:
    """Browse scripts may silence stderr. They must not write or start tmux."""

    cleaned = script.replace("2>/dev/null", "")
    forbidden = (
        "tmux",
        "mkdir",
        "rm ",
        "rm\n",
        "mv ",
        "cp ",
        "touch ",
        "chmod ",
        "send-keys",
        "new-window",
        "split-window",
        ">",
    )
    return not any(marker in cleaned for marker in forbidden)


def remote_list_script(path: str) -> str:
    target = shlex.quote(path)
    return "\n".join([
        "set -u",
        f"target={target}",
        'if [ ! -e "$target" ]; then printf "%s\\n" "__STATUS__ missing"; exit 0; fi',
        'if [ ! -d "$target" ]; then printf "%s\\n" "__STATUS__ notdir"; exit 0; fi',
        'if [ ! -r "$target" ] || [ ! -x "$target" ]; then printf "%s\\n" "__STATUS__ denied"; exit 0; fi',
        'canon=$(cd "$target" && pwd -P) || { printf "%s\\n" "__STATUS__ denied"; exit 0; }',
        'printf "%s %s\\n" "__STATUS__" "ok"',
        'printf "%s %s\\n" "__PATH__" "$canon"',
        'if [ -e "$canon/.git" ]; then printf "%s\\n" "__GIT__ 1"; else printf "%s\\n" "__GIT__ 0"; fi',
        'find "$canon" -mindepth 1 -maxdepth 1 -type d ! -name ".*" 2>/dev/null | while IFS= read -r child; do',
        '  name=$(basename "$child")',
        '  case "$name" in node_modules|.venv|venv|__pycache__|dist|build|target|.git) continue ;; esac',
        '  git=0',
        '  if [ -e "$child/.git" ]; then git=1; fi',
        '  printf "%s\\t%s\\t%s\\n" "$name" "$child" "$git"',
        "done",
    ])


def remote_validate_script(path: str) -> str:
    target = shlex.quote(path)
    return "\n".join([
        "set -u",
        f"target={target}",
        'if [ ! -e "$target" ]; then printf "%s\\n" "__STATUS__ missing"; exit 0; fi',
        'if [ ! -d "$target" ]; then printf "%s\\n" "__STATUS__ notdir"; exit 0; fi',
        'if [ ! -r "$target" ] || [ ! -x "$target" ]; then printf "%s\\n" "__STATUS__ denied"; exit 0; fi',
        'canon=$(cd "$target" && pwd -P) || { printf "%s\\n" "__STATUS__ denied"; exit 0; }',
        'printf "%s %s\\n" "__STATUS__" "ok"',
        'printf "%s %s\\n" "__PATH__" "$canon"',
        'if [ -e "$canon/.git" ]; then git=1; else git=0; fi',
        'printf "%s %s\\n" "__GIT__" "$git"',
        'printf "%s %s\\n" "__NAME__" "$(basename "$canon")"',
    ])


def remote_roots_script() -> str:
    names = " ".join(shlex.quote(name) for name in _CANDIDATE_ROOT_NAMES)
    return "\n".join([
        "set -u",
        'printf "%s %s\\n" "__HOME__" "$HOME"',
        f"for name in {names}; do",
        '  if [ -d "$HOME/$name" ]; then printf "%s %s\\n" "__ROOT__" "$HOME/$name"; fi',
        "done",
        'if [ -d /mnt ]; then',
        "  find /mnt -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while IFS= read -r drive; do",
        '    if [ -r "$drive" ] && [ -x "$drive" ]; then printf "%s %s\\n" "__MNT__" "$drive"; fi',
        "  done",
        "fi",
    ])


def remote_catalog_script(max_depth: int = SEARCH_MAX_DEPTH, limit: int = SEARCH_LIMIT) -> str:
    """Git repos first, then other directories, one bounded SSH find."""

    depth = int(max_depth)
    cap = int(limit)
    names = " ".join(shlex.quote(name) for name in _CANDIDATE_ROOT_NAMES)
    prune = (
        "\\( -name node_modules -o -name .venv -o -name venv -o -name __pycache__ "
        "-o -name .cache -o -name dist -o -name build -o -name .next -o -name .turbo -o -name .tox \\) -prune -o"
    )
    return "\n".join([
        "set -u",
        "set --",
        f"for name in {names}; do",
        '  if [ -d "$HOME/$name" ]; then set -- "$@" "$HOME/$name"; fi',
        "done",
        '[ "$#" -eq 0 ] && exit 0',
        "{",
        f'  find "$@" -maxdepth {depth} {prune} -type d -name .git -print 2>/dev/null | while IFS= read -r gitdir; do',
        '    dir=$(dirname "$gitdir")',
        '    printf "%s\\t%s\\t1\\n" "$(basename "$dir")" "$dir"',
        "  done",
        f'  find "$@" -maxdepth {depth} {prune} -type d ! -name ".*" -print 2>/dev/null | while IFS= read -r dir; do',
        '    if [ -e "$dir/.git" ]; then continue; fi',
        '    printf "%s\\t%s\\t0\\n" "$(basename "$dir")" "$dir"',
        "  done",
        f"}} | head -n {cap}",
    ])


def remote_classify_script(paths: Sequence[str]) -> str:
    """One SSH round trip that classifies an existing recent-path list."""

    lines = ["set -u"]
    for path in paths:
        quoted = shlex.quote(path)
        lines.extend([
            f"target={quoted}",
            'if [ -d "$target" ] && [ -r "$target" ] && [ -x "$target" ]; then',
            '  canon=$(cd "$target" && pwd -P) || canon="$target"',
            '  git=0',
            '  if [ -e "$canon/.git" ]; then git=1; fi',
            '  printf "%s\\t%s\\t%s\\n" "$(basename "$canon")" "$canon" "$git"',
            "else",
            f'  printf "%s\\t%s\\t0\\n" "__MISSING__" {quoted}',
            "fi",
        ])
    return "\n".join(lines)


def _status_map(status: str) -> str:
    return {"missing": "not_found", "notdir": "not_dir", "denied": "denied"}.get(status, "unreachable")


def _parse_validate(requested: str, host: str, stdout: str) -> PathCheck:
    status = ""
    canon = ""
    git = False
    name = ""
    for line in (stdout or "").splitlines():
        if line.startswith("__STATUS__ "):
            status = line.split(" ", 1)[1].strip()
        elif line.startswith("__PATH__ "):
            canon = line.split(" ", 1)[1]
        elif line.startswith("__GIT__ "):
            git = line.split(" ", 1)[1].strip() == "1"
        elif line.startswith("__NAME__ "):
            name = line.split(" ", 1)[1]
    if status != "ok" or not canon:
        return PathCheck(False, error=BrowseError(_status_map(status), requested, host))
    return PathCheck(True, entry=ProjectEntry(name=name or directory_name(canon), path=canon, is_git=git))


def _parse_list(requested: str, host: str, stdout: str) -> DirectoryPage:
    status = ""
    canon = ""
    git = False
    children: List[ProjectEntry] = []
    for line in (stdout or "").splitlines():
        if line.startswith("__STATUS__ "):
            status = line.split(" ", 1)[1].strip()
            continue
        if line.startswith("__PATH__ "):
            canon = line.split(" ", 1)[1]
            continue
        if line.startswith("__GIT__ "):
            git = line.split(" ", 1)[1].strip() == "1"
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1].startswith("/"):
            continue
        children.append(ProjectEntry(name=parts[0] or directory_name(parts[1]), path=parts[1], is_git=parts[2] == "1"))
    if status != "ok" or not canon:
        return DirectoryPage(path=requested, error=BrowseError(_status_map(status), requested, host))
    children.sort(key=lambda entry: entry.name.lower())
    return DirectoryPage(path=canon, is_git=git, children=tuple(children))


SshRunner = Callable[[str, str, float], object]


def _default_ssh(host: str, script: str, timeout: float):
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout))}", host, script],
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "error"


def _ssh_text(host: str, script: str, timeout: float, runner: Optional[SshRunner]) -> Tuple[str, str]:
    """Returns ``(kind, stdout)``. ``kind`` is ``ok``, ``timeout``, or ``error``."""

    if not script_is_read_only(script):
        return "error", ""
    outcome = (runner or _default_ssh)(host, script, timeout)
    if outcome in ("timeout", "error") or outcome is None:
        return ("timeout" if outcome == "timeout" else "error"), ""
    if not isinstance(outcome, subprocess.CompletedProcess):
        return "error", ""
    stdout = outcome.stdout or ""
    if outcome.returncode != 0 and "__STATUS__" not in stdout and "__HOME__" not in stdout and "\t" not in stdout:
        return "error", ""
    return "ok", stdout


def validate_remote_path(
    host: str,
    raw: str,
    *,
    timeout: float = 6.0,
    runner: Optional[SshRunner] = None,
) -> PathCheck:
    requested = (raw or "").strip()
    if not requested:
        return PathCheck(False, error=BrowseError("empty", requested, host))
    kind, stdout = _ssh_text(host, remote_validate_script(requested), timeout, runner)
    if kind != "ok":
        return PathCheck(False, error=BrowseError("unreachable", requested, host))
    return _parse_validate(requested, host, stdout)


def list_remote_children(
    host: str,
    path: str,
    *,
    cache: Optional[DirectoryCache] = None,
    timeout: float = 6.0,
    runner: Optional[SshRunner] = None,
) -> DirectoryPage:
    if cache is not None:
        cached = cache.get(host, path)
        if cached is not None:
            return cached
    kind, stdout = _ssh_text(host, remote_list_script(path), timeout, runner)
    if kind != "ok":
        return DirectoryPage(path=path, error=BrowseError("unreachable", path, host))
    page = _parse_list(path, host, stdout)
    if cache is not None and page.ok:
        cache.put(host, path, page)
        if page.path != path:
            cache.put(host, page.path, page)
    return page


def list_children(
    host: str,
    path: str,
    *,
    is_remote: bool,
    cache: Optional[DirectoryCache] = None,
    timeout: float = 6.0,
    runner: Optional[SshRunner] = None,
) -> DirectoryPage:
    if cache is not None:
        cached = cache.get(host, path)
        if cached is not None:
            return cached
    if is_remote:
        return list_remote_children(host, path, cache=cache, timeout=timeout, runner=runner)
    page = list_local_children(path)
    if cache is not None and page.ok:
        cache.put(host, path, page)
        if page.path != path:
            cache.put(host, page.path, page)
    return page


def remote_start_roots(
    host: str,
    recent: Sequence[str],
    *,
    timeout: float = 6.0,
    runner: Optional[SshRunner] = None,
) -> Tuple[List[str], Optional[BrowseError]]:
    kind, stdout = _ssh_text(host, remote_roots_script(), timeout, runner)
    if kind != "ok":
        return [], BrowseError("unreachable", "", host)
    configured: List[str] = []
    mounts: List[str] = []
    home = ""
    for line in stdout.splitlines():
        if line.startswith("__HOME__ "):
            home = line.split(" ", 1)[1].strip()
        elif line.startswith("__ROOT__ "):
            configured.append(line.split(" ", 1)[1].strip())
        elif line.startswith("__MNT__ "):
            mounts.append(line.split(" ", 1)[1].strip())
    ordered: List[str] = []
    for path in list(configured) + list(recent) + ([home] if home else []) + mounts:
        if path and path not in ordered:
            ordered.append(path)
    return ordered, None


def collect_remote_catalog(
    host: str,
    *,
    cache: Optional[dict] = None,
    timeout: float = 6.0,
    runner: Optional[SshRunner] = None,
    max_depth: int = SEARCH_MAX_DEPTH,
    limit: int = SEARCH_LIMIT,
) -> Tuple[List[ProjectEntry], Optional[BrowseError]]:
    """One bounded SSH listing. Filtering happens locally afterward."""

    if cache is not None and host in cache:
        return cache[host], None
    kind, stdout = _ssh_text(host, remote_catalog_script(max_depth, limit), timeout, runner)
    if kind != "ok":
        return [], BrowseError("unreachable", "", host)
    git: List[ProjectEntry] = []
    folders: List[ProjectEntry] = []
    seen = set()
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1].startswith("/"):
            continue
        if parts[1] in seen:
            continue
        seen.add(parts[1])
        entry = ProjectEntry(name=parts[0] or directory_name(parts[1]), path=parts[1], is_git=parts[2] == "1")
        if entry.is_git:
            git.append(entry)
        else:
            folders.append(entry)
        if len(git) + len(folders) >= limit:
            break
    git.sort(key=lambda entry: entry.name.lower())
    folders.sort(key=lambda entry: entry.name.lower())
    entries = git + folders
    if cache is not None:
        cache[host] = entries
    return entries, None


def classify_remote_paths(
    host: str,
    paths: Sequence[str],
    *,
    timeout: float = 6.0,
    runner: Optional[SshRunner] = None,
) -> Tuple[List[ProjectEntry], Optional[BrowseError]]:
    """Classify recent paths in one SSH call. Missing paths keep the typed path."""

    if not paths:
        return [], None
    kind, stdout = _ssh_text(host, remote_classify_script(paths), timeout, runner)
    if kind != "ok":
        return [], BrowseError("unreachable", "", host)
    entries: List[ProjectEntry] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, path, git = parts
        if name == "__MISSING__":
            entries.append(ProjectEntry(name=directory_name(path), path=path, is_git=False))
        else:
            entries.append(ProjectEntry(name=name or directory_name(path), path=path, is_git=git == "1"))
    return entries, None
