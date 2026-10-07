"""Expand and collapse a folder tree without scanning ahead.

A node is fetched only when it is opened. Collapsed folders keep no
children. Each host keeps its own tree and its own directory cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .browse import DirectoryCache
from .discovery import ProjectEntry


@dataclass
class TreeNode:
    name: str
    path: str
    is_git: bool
    expanded: bool = False
    loaded: bool = False
    children: List["TreeNode"] = field(default_factory=list)


@dataclass(frozen=True)
class TreeRow:
    node: TreeNode
    depth: int


def row_label(node: TreeNode) -> str:
    """Disclosure triangle, and a diamond when the directory is a git repo."""

    twist = "▾" if node.expanded else "▸"
    if node.is_git:
        return f"{twist} ◆ {node.name}"
    return f"{twist} {node.name}"


def format_row(row: TreeRow) -> str:
    return ("  " * row.depth) + row_label(row.node)


def detail_fields(node: TreeNode) -> tuple[str, str, str]:
    """Name, full path, and Git or Folder. The path is never shortened here."""

    return node.name, node.path, "Git" if node.is_git else "Folder"


def command_for(name: str) -> str:
    """Key meaning. Enter expands. Space is the only select."""

    return {
        "up": "move-up",
        "down": "move-down",
        "right": "expand",
        "enter": "expand",
        "left": "collapse",
        "space": "select",
        "slash": "filter",
        "b": "path",
        "r": "refresh",
        "f": "find",
        "h": "host",
        "l": "root",
        "esc": "back",
    }.get(name, "")


def _matches(node: TreeNode, query: str) -> bool:
    text = query.lower()
    if text in node.name.lower() or text in node.path.lower():
        return True
    if not node.expanded:
        return False
    return any(_matches(child, query) for child in node.children)


class FolderTree:
    """One host, one root, and the folders the user has actually opened."""

    def __init__(self, host: str, root: ProjectEntry):
        self.host = host
        self.root = TreeNode(root.name, root.path, root.is_git)
        self.selected = 0
        self.query = ""

    def visible_rows(self) -> List[TreeRow]:
        rows: List[TreeRow] = []

        def emit(node: TreeNode, depth: int) -> None:
            if self.query and not _matches(node, self.query):
                return
            rows.append(TreeRow(node, depth))
            if not node.expanded:
                return
            for child in node.children:
                emit(child, depth + 1)

        emit(self.root, 0)
        return rows

    def clamp(self) -> TreeRow:
        rows = self.visible_rows()
        if not rows:
            self.selected = 0
            return TreeRow(self.root, 0)
        self.selected = max(0, min(self.selected, len(rows) - 1))
        return rows[self.selected]

    def move(self, delta: int) -> None:
        self.clamp()
        self.selected += delta
        self.clamp()

    def selected_node(self) -> TreeNode:
        return self.clamp().node

    def expand_action(self) -> str:
        """``fetch`` when this folder has never been read, else open it.

        Does not read children of the children. An already open folder
        moves onto its first child instead of selecting the workspace.
        """

        node = self.selected_node()
        if node.expanded:
            if node.children:
                self.selected += 1
                self.clamp()
            return "into"
        if node.loaded:
            node.expanded = True
            return "cached"
        return "fetch"

    def apply_children(self, path: str, children: Sequence[ProjectEntry], *, preserve: bool = False) -> None:
        node = self._find(path)
        if node is None:
            return
        previous = {child.path: child for child in node.children} if preserve else {}
        built: List[TreeNode] = []
        for entry in children:
            kept = previous.get(entry.path)
            if kept is not None:
                kept.name = entry.name
                kept.is_git = entry.is_git
                built.append(kept)
            else:
                built.append(TreeNode(entry.name, entry.path, entry.is_git))
        node.children = built
        node.loaded = True
        node.expanded = True

    def collapse_action(self) -> str:
        """Close the folder, or move to its parent when it is already closed."""

        node = self.selected_node()
        if node.expanded:
            node.expanded = False
            return "collapsed"
        parent = self._parent(node)
        if parent is None:
            return "root"
        rows = self.visible_rows()
        for index, row in enumerate(rows):
            if row.node is parent:
                self.selected = index
                break
        return "parent"

    def refresh_path(self) -> str:
        return self.selected_node().path

    def as_entry(self) -> ProjectEntry:
        node = self.selected_node()
        return ProjectEntry(node.name, node.path, node.is_git)

    def loaded_paths(self) -> List[str]:
        found: List[str] = []

        def walk(node: TreeNode) -> None:
            if node.loaded:
                found.append(node.path)
            for child in node.children:
                walk(child)

        walk(self.root)
        return found

    def _find(self, path: str) -> Optional[TreeNode]:
        def walk(node: TreeNode) -> Optional[TreeNode]:
            if node.path == path:
                return node
            for child in node.children:
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(self.root)

    def _parent(self, target: TreeNode) -> Optional[TreeNode]:
        def walk(node: TreeNode) -> Optional[TreeNode]:
            for child in node.children:
                if child is target:
                    return node
                found = walk(child)
                if found is not None:
                    return found
            return None

        if target is self.root:
            return None
        return walk(self.root)


def visible_window(rows: Sequence[TreeRow], selected: int, limit: int) -> List[TreeRow]:
    """The rows that fit on screen. The rest stay in the model, not on it."""

    if limit < 1:
        limit = 1
    if not rows:
        return []
    selected = max(0, min(selected, len(rows) - 1))
    top = 0 if selected < limit else selected - limit + 1
    return list(rows[top : top + limit])


class TreeBook:
    """Expanded trees and directory caches, separated by host."""

    def __init__(self):
        self.trees: Dict[str, FolderTree] = {}
        self.caches: Dict[str, DirectoryCache] = {}

    def cache_for(self, host: str) -> DirectoryCache:
        cache = self.caches.get(host)
        if cache is None:
            cache = DirectoryCache()
            self.caches[host] = cache
        return cache

    def remember(self, host: str, tree: FolderTree) -> None:
        self.trees[host] = tree

    def recall(self, host: str) -> Optional[FolderTree]:
        return self.trees.get(host)
