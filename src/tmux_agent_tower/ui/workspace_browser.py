"""Host, then a workspace, then agent and placement.

Browse reads directories only. tmux is used after the user confirms create.
"""

from __future__ import annotations

import curses
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..i18n import t
from ..launcher.browse import (
    BrowseError,
    DirectoryCache,
    ProjectEntry,
    classify_remote_paths,
    collect_local_catalog,
    collect_remote_catalog,
    directory_name,
    entry_detail,
    filter_entries,
    format_entry_lines,
    kind_label,
    list_children,
    local_start_roots,
    remote_start_roots,
    validate_local_path,
    validate_remote_path,
)
from ..launcher.config import load_config, resolve_agent_command
from ..launcher.discovery import load_recent
from ..launcher.plan import LaunchPlan, LaunchPlanError, WorkspaceRef, execute_launch_plan
from ..state.overrides import ROLE_IDS
from ..launcher.treeview import FolderTree, TreeBook, format_row, visible_window
from ..launcher.spawn import SpawnResult, SpawnTarget
from ..tmux import structure
from .render import use_narrow_layout
from .widgets import (
    is_backspace,
    is_enter,
    is_escape,
    matches_letter,
    prompt_text,
    read_key,
    run_list_picker,
    safe_add,
    show_message_screen,
)

CREATE_AGENT_ORDER = ["Codex", "Claude", "Cursor", "OpenCode", "Grok", "Shell"]
QUICK_AGENT_ORDER = ["Codex", "Claude", "Grok", "OpenCode", "Cursor"]

WINDOW_LAYOUT = {
    "auto": "tiled",
    "horizontal": "even-horizontal",
    "vertical": "even-vertical",
    "main-horizontal": "main-horizontal",
    "main-vertical": "main-vertical",
}
PANE_LAYOUT = {
    "auto": "auto",
    "horizontal": "horizontal",
    "vertical": "vertical",
}


@dataclass(frozen=True)
class WorkspacePick:
    host_key: str
    host_label: str
    is_remote: bool
    entry: ProjectEntry


def host_choices(local_host: str, remote_hosts: Sequence[dict]) -> List[Tuple[str, str, bool]]:
    """Every host is a real choice, including when only this machine is configured."""

    choices = [(local_host, local_host, False)]
    for host in remote_hosts:
        alias = host.get("alias") or ""
        if not alias:
            continue
        choices.append((alias, host.get("name") or alias, True))
    return choices


def placement_choices(is_remote: bool) -> List[str]:
    if is_remote:
        return ["new"]
    return ["new", "current", "pick"]


def browse_error_text(error: BrowseError, host_label: str) -> str:
    host = host_label or error.host or ""
    if error.code == "unreachable":
        return t("browser.unreachable", host=host)
    if error.code == "not_dir":
        return t("browser.not_dir", path=error.path)
    if error.code == "denied":
        return t("browser.denied", path=error.path)
    if error.code == "empty":
        return t("browser.empty_path")
    return t("browser.not_found", path=error.path)


def launch_workspaces(
    *,
    session: str,
    state_dir: Path,
    picks: Sequence[WorkspacePick],
    agent: str,
    placement: str,
    layout_choice: str,
    window_id: str = "",
    remote_session: str = "",
    overrides=None,
    agents_cfg: Optional[dict] = None,
) -> list:
    """Compatibility wrapper; every launch still goes through LaunchPlan."""

    if not picks:
        return []
    cfg = agents_cfg if agents_cfg is not None else load_config()["agents"]
    first = picks[0]
    plan = LaunchPlan(
        host_key=first.host_key,
        host_label=first.host_label,
        is_remote=first.is_remote,
        workspaces=tuple(WorkspaceRef(pick.entry.name, pick.entry.path) for pick in picks),
        agents=(agent,),
        layout=WINDOW_LAYOUT.get(layout_choice, layout_choice),
        placement=(
            placement
            if not first.is_remote or (remote_session and window_id)
            else "new"
        ),
        window_id=window_id,
        remote_session=remote_session,
        pane_direction=PANE_LAYOUT.get(layout_choice, layout_choice),
    )
    try:
        return execute_launch_plan(
            plan,
            session=session,
            state_dir=state_dir,
            agents_cfg=cfg,
            overrides=overrides,
        )
    except LaunchPlanError as exc:
        return [
            SpawnResult(SpawnTarget(pick.entry.path, pick.entry.name, agent), False, str(exc))
            for pick in picks
        ]


def _flash(stdscr, text: str) -> None:
    stdscr.erase()
    safe_add(stdscr, 1, 2, text, curses.A_BOLD)
    stdscr.refresh()


def _show_error(stdscr, error: BrowseError, host_label: str) -> None:
    show_message_screen(stdscr, t("browser.failed"), [browse_error_text(error, host_label)])


def _pick_host(stdscr, tower) -> Optional[Tuple[str, str, bool]]:
    choices = host_choices(tower.local_host, tower.remote_hosts)
    if len(choices) == 1:
        return choices[0]
    pick = run_list_picker(
        stdscr,
        t("browser.where"),
        [(key, label) for key, label, _remote in choices],
        footer_hint=t("wizard.hint_list"),
    )
    if pick.cancelled or pick.selected_key is None:
        return None
    for key, label, is_remote in choices:
        if key == pick.selected_key:
            return key, label, is_remote
    return None


def _pick_from_entries(
    stdscr,
    title: str,
    entries: Sequence[ProjectEntry],
    *,
    preamble: Sequence[str] = (),
    searchable: bool = False,
    extra_keys: Optional[dict] = None,
    footer: str = "",
) -> Tuple[str, Optional[ProjectEntry]]:
    """Scroll the full filtered list. Returns ``(action, entry)``.

    ``action`` is ``picked``, ``cancelled``, or an extra-key tag.
    """

    extra_keys = extra_keys or {}
    search = ""
    editing = False
    selected = 0
    stdscr.timeout(-1)
    try:
        while True:
            visible = filter_entries(entries, search)
            selected = max(0, min(selected, len(visible) - 1)) if visible else 0
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            safe_add(stdscr, 0, 2, title, curses.A_BOLD)
            row_y = 2
            for line in preamble:
                safe_add(stdscr, row_y, 2, line)
                row_y += 1
            if preamble:
                row_y += 1
            if searchable:
                cursor = "_" if editing else ""
                safe_add(stdscr, row_y, 2, f'{t("wizard.search_label")} {search}{cursor}', curses.A_DIM)
                row_y += 2

            row_h = 2 if use_narrow_layout(width) else 1
            if not use_narrow_layout(width):
                for entry in visible:
                    if len(format_entry_lines(entry.name, entry.path, entry.is_git, width)) > 1:
                        row_h = 2
                        break
            max_rows = max(1, (height - row_y - 4) // row_h)
            top = 0
            if selected >= max_rows:
                top = selected - max_rows + 1
            if not visible:
                safe_add(stdscr, row_y, 2, t("wizard.no_matches"), curses.A_DIM)
            for offset, entry in enumerate(visible[top : top + max_rows]):
                lines = format_entry_lines(entry.name, entry.path, entry.is_git, width)
                attr = curses.A_REVERSE if top + offset == selected else 0
                marker = "> " if top + offset == selected else "  "
                for line_index, line in enumerate(lines[:row_h]):
                    safe_add(stdscr, row_y + offset * row_h + line_index, 0, f"{marker if line_index == 0 else '  '}{line}", attr)
            current = visible[selected] if visible else None
            safe_add(stdscr, height - 3, 2, entry_detail(current.path) if current else "", curses.A_DIM)
            safe_add(stdscr, height - 2, 2, footer or t("browser.list_hint"), curses.A_DIM)
            stdscr.refresh()
            key = read_key(stdscr)

            if editing:
                if is_escape(key) or is_enter(key):
                    editing = False
                    continue
                if is_backspace(key):
                    search = search[:-1]
                    selected = 0
                    continue
                if isinstance(key, str) and key.isprintable():
                    search += key
                    selected = 0
                continue
            if is_escape(key):
                return "cancelled", None
            if key == curses.KEY_UP:
                selected -= 1
                continue
            if key == curses.KEY_DOWN:
                selected += 1
                continue
            if searchable and key == "/":
                editing = True
                continue
            if isinstance(key, str) and key.lower() in extra_keys:
                return extra_keys[key.lower()], current
            if is_enter(key) and current is not None:
                return "picked", current
    finally:
        stdscr.timeout(200)


def _validate(host_key: str, is_remote: bool, raw: str) -> Tuple[Optional[ProjectEntry], Optional[BrowseError]]:
    if is_remote:
        check = validate_remote_path(host_key, raw)
    else:
        check = validate_local_path(raw)
    if not check.ok or check.entry is None:
        return None, check.error or BrowseError("not_found", raw, host_key)
    return check.entry, None


def _ask_path(stdscr, host_key: str, host_label: str, is_remote: bool) -> Optional[ProjectEntry]:
    while True:
        typed = prompt_text(
            stdscr,
            t("browser.path_prompt"),
            context_lines=[t("browser.host", host=host_label), t("browser.path_examples")],
        )
        if typed is None:
            return None
        entry, error = _validate(host_key, is_remote, typed)
        if error is not None or entry is None:
            _show_error(stdscr, error or BrowseError("not_found", typed, host_key), host_label)
            continue
        return entry


def _recent_entries(host_key: str, host_label: str, is_remote: bool, state_dir: Path, stdscr) -> Optional[List[ProjectEntry]]:
    paths = load_recent(state_dir, host_key)
    if not paths:
        return []
    if not is_remote:
        entries = []
        for path in paths:
            check = validate_local_path(path)
            if check.ok and check.entry is not None:
                entries.append(check.entry)
            else:
                entries.append(ProjectEntry(directory_name(path), path, False))
        return entries
    _flash(stdscr, t("browser.loading"))
    entries, error = classify_remote_paths(host_key, paths)
    if error is not None:
        _show_error(stdscr, error, host_label)
        return [ProjectEntry(directory_name(path), path, False) for path in paths]
    return entries


def _search(stdscr, host_key: str, host_label: str, is_remote: bool, roots: Sequence[str], catalog_cache: dict) -> Optional[ProjectEntry]:
    if host_key not in catalog_cache:
        _flash(stdscr, t("browser.loading"))
        if is_remote:
            entries, error = collect_remote_catalog(host_key, cache=catalog_cache)
            if error is not None:
                _show_error(stdscr, error, host_label)
                return None
        else:
            catalog_cache[host_key] = collect_local_catalog(roots)
    while True:
        action, entry = _pick_from_entries(
            stdscr,
            t("browser.search"),
            catalog_cache.get(host_key) or [],
            preamble=[t("browser.host", host=host_label)],
            searchable=True,
            extra_keys={"b": "manual"},
            footer=t("browser.search_hint"),
        )
        if action == "cancelled":
            return None
        if action == "manual":
            manual = _ask_path(stdscr, host_key, host_label, is_remote)
            if manual is not None:
                return manual
            continue
        if entry is None:
            continue
        if is_remote:
            checked, error = _validate(host_key, True, entry.path)
            if error is not None or checked is None:
                _show_error(stdscr, error or BrowseError("not_found", entry.path, host_key), host_label)
                continue
            return checked
        return entry


@dataclass(frozen=True)
class BrowseResult:
    kind: str
    entry: Optional[ProjectEntry] = None


def _tree(
    stdscr,
    host_key: str,
    host_label: str,
    is_remote: bool,
    state_dir: Path,
    roots: Sequence[str],
    book: TreeBook,
    catalog_cache: dict,
) -> BrowseResult:
    remembered = book.recall(host_key)
    if remembered is not None:
        return _browse_tree(stdscr, host_key, host_label, is_remote, state_dir, roots, book, remembered, catalog_cache)
    chosen = _pick_root(stdscr, host_key, host_label, is_remote, state_dir, roots)
    if chosen is None:
        return BrowseResult("back")
    tree = FolderTree(host_key, chosen)
    book.remember(host_key, tree)
    opened = _load_node(stdscr, host_key, host_label, is_remote, tree, tree.root.path, book.cache_for(host_key), refresh=False)
    if not opened:
        book.trees.pop(host_key, None)
        return BrowseResult("back")
    return _browse_tree(stdscr, host_key, host_label, is_remote, state_dir, roots, book, tree, catalog_cache)


def _pick_root(stdscr, host_key, host_label, is_remote, state_dir, roots) -> Optional[ProjectEntry]:
    if is_remote:
        _flash(stdscr, t("browser.loading"))
        start, error = remote_start_roots(host_key, load_recent(state_dir, host_key))
        if error is not None:
            _show_error(stdscr, error, host_label)
            start = [path for path in load_recent(state_dir, host_key)]
    else:
        start = local_start_roots(roots, load_recent(state_dir, host_key))
    if not start:
        show_message_screen(stdscr, t("browser.tree"), [t("browser.no_roots")])
        return _ask_path(stdscr, host_key, host_label, is_remote)

    start_entries = [ProjectEntry(directory_name(path), path, False) for path in start]
    action, chosen = _pick_from_entries(
        stdscr,
        t("browser.pick_root"),
        start_entries,
        preamble=[t("browser.host", host=host_label)],
        extra_keys={"b": "manual"},
        footer=t("browser.list_hint"),
    )
    if action == "cancelled" or (action != "manual" and chosen is None):
        return None
    if action == "manual":
        return _ask_path(stdscr, host_key, host_label, is_remote)
    checked, error = _validate(host_key, is_remote, chosen.path)
    if error is not None or checked is None:
        _show_error(stdscr, error or BrowseError("not_found", chosen.path, host_key), host_label)
        return None
    return checked


def _load_node(stdscr, host_key, host_label, is_remote, tree: FolderTree, path: str, cache: DirectoryCache, *, refresh: bool) -> bool:
    if refresh:
        cache.drop(host_key, path, "heavy")
    page = list_children(host_key, path, is_remote=is_remote, cache=None if refresh else cache, include_heavy=True)
    if not page.ok:
        _show_error(stdscr, page.error or BrowseError("unreachable", path, host_key), host_label)
        return False
    if refresh:
        cache.put(host_key, page.path, page, "heavy")
    tree.apply_children(path, page.children, preserve=refresh)
    return True


def _browse_tree(
    stdscr,
    host_key: str,
    host_label: str,
    is_remote: bool,
    state_dir: Path,
    roots: Sequence[str],
    book: TreeBook,
    tree: FolderTree,
    catalog_cache: dict,
) -> BrowseResult:
    cache = book.cache_for(host_key)
    editing = False
    stdscr.timeout(-1)
    try:
        while True:
            rows = tree.visible_rows()
            current = tree.clamp()
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            safe_add(stdscr, 0, 2, t("browser.tree"), curses.A_BOLD)
            safe_add(stdscr, 2, 2, t("browser.host", host=host_label))
            safe_add(stdscr, 3, 2, t("browser.root_line", path=tree.root.path))
            row_y = 5
            if editing or tree.query:
                cursor = "_" if editing else ""
                safe_add(stdscr, row_y, 2, f'{t("wizard.search_label")} {tree.query}{cursor}', curses.A_DIM)
                row_y += 2
            max_rows = max(1, height - row_y - 6)
            window = visible_window(rows, tree.selected, max_rows)
            top = 0 if tree.selected < max_rows else tree.selected - max_rows + 1
            if not window:
                safe_add(stdscr, row_y, 2, t("browser.empty_dir"), curses.A_DIM)
            for offset, row in enumerate(window):
                attr = curses.A_REVERSE if top + offset == tree.selected else 0
                marker = "> " if top + offset == tree.selected else "  "
                safe_add(stdscr, row_y + offset, 0, marker + format_row(row), attr)
            node = current.node
            safe_add(stdscr, height - 5, 2, t("browser.detail_name", name=node.name))
            safe_add(stdscr, height - 4, 2, t("browser.detail_path", path=node.path))
            safe_add(stdscr, height - 3, 2, t("browser.detail_kind", kind=kind_label(node.is_git)))
            safe_add(stdscr, height - 2, 2, t("browser.tree_hint"), curses.A_DIM)
            stdscr.refresh()
            key = read_key(stdscr)

            if editing:
                if is_escape(key) or is_enter(key):
                    editing = False
                    continue
                if is_backspace(key):
                    tree.query = tree.query[:-1]
                    tree.selected = 0
                    continue
                if isinstance(key, str) and key.isprintable():
                    tree.query += key
                    tree.selected = 0
                continue
            if is_escape(key):
                return BrowseResult("back")
            if key == curses.KEY_UP:
                tree.move(-1)
                continue
            if key == curses.KEY_DOWN:
                tree.move(1)
                continue
            if key == curses.KEY_LEFT:
                tree.collapse_action()
                continue
            if key == curses.KEY_RIGHT or is_enter(key):
                action = tree.expand_action()
                if action == "fetch":
                    _load_node(stdscr, host_key, host_label, is_remote, tree, tree.selected_node().path, cache, refresh=False)
                continue
            if key == "/":
                editing = True
                continue
            if matches_letter(key, "r"):
                _load_node(stdscr, host_key, host_label, is_remote, tree, tree.refresh_path(), cache, refresh=True)
                continue
            if matches_letter(key, "b"):
                manual = _ask_path(stdscr, host_key, host_label, is_remote)
                if manual is not None:
                    return BrowseResult("entry", manual)
                continue
            if matches_letter(key, "f"):
                found = _search(stdscr, host_key, host_label, is_remote, roots, catalog_cache)
                if found is not None:
                    return BrowseResult("entry", found)
                continue
            if matches_letter(key, "h"):
                return BrowseResult("host")
            if matches_letter(key, "l"):
                chosen = _pick_root(stdscr, host_key, host_label, is_remote, state_dir, roots)
                if chosen is None:
                    continue
                tree = FolderTree(host_key, chosen)
                book.remember(host_key, tree)
                _load_node(stdscr, host_key, host_label, is_remote, tree, tree.root.path, cache, refresh=False)
                continue
            if key == " ":
                return BrowseResult("entry", tree.as_entry())
    finally:
        stdscr.timeout(200)


def pick_workspaces(stdscr, tower, state_dir: Path, *, multi: bool, open_tree: bool = False) -> Optional[List[WorkspacePick]]:
    """Host first. A new task opens the tree; other creates can still use the menu."""

    book = TreeBook()
    while True:
        host = _pick_host(stdscr, tower)
        if host is None:
            return None
        host_key, host_label, is_remote = host
        if open_tree and not multi:
            result = _tree(stdscr, host_key, host_label, is_remote, state_dir, load_config()["project_roots"], book, {})
            if result.kind == "entry" and result.entry is not None:
                return [WorkspacePick(host_key, host_label, is_remote, result.entry)]
            continue
        picked = _browser_home(stdscr, tower, state_dir, host_key, host_label, is_remote, multi, book)
        if picked is None:
            continue
        return picked


def pick_project_for_target(stdscr, tower, state_dir: Path, row: dict) -> Optional[WorkspacePick]:
    """Pick a project on the pane's current execution host."""

    if row.get("remote"):
        return None
    if row.get("transport") == "ssh":
        host_key = str(row.get("transport_target") or row.get("execution_host") or "")
        if not host_key:
            return None
        remote = next((host for host in tower.remote_hosts if host.get("alias") == host_key), {})
        host_label = str(remote.get("name") or host_key)
        is_remote = True
    else:
        host_key = tower.local_host
        host_label = tower.local_host
        is_remote = False
    picked = _browser_home(
        stdscr, tower, state_dir, host_key, host_label, is_remote, False, TreeBook()
    )
    return picked[0] if picked else None


def _browser_home(stdscr, tower, state_dir, host_key, host_label, is_remote, multi, book: TreeBook) -> Optional[List[WorkspacePick]]:
    cfg = load_config()
    roots = cfg["project_roots"]
    basket: List[ProjectEntry] = []
    catalog_cache: dict = {}
    while True:
        items = [
            ("tree", t("browser.tree")),
            ("recent", t("browser.recent")),
            ("search", t("browser.search")),
            ("path", t("browser.manual")),
        ]
        if multi and basket:
            items.append(("done", t("browser.done")))
        preamble = [t("browser.host", host=host_label)]
        for entry in basket:
            preamble.append(entry.name)
            preamble.append(entry.path)
        pick = run_list_picker(
            stdscr,
            t("browser.home_title"),
            items,
            footer_hint=t("wizard.hint_list"),
            preamble=preamble,
        )
        if pick.cancelled or pick.selected_key is None:
            return None
        entry: Optional[ProjectEntry] = None
        if pick.selected_key == "done":
            return [WorkspacePick(host_key, host_label, is_remote, item) for item in basket]
        if pick.selected_key == "recent":
            entries = _recent_entries(host_key, host_label, is_remote, state_dir, stdscr)
            if entries is None:
                continue
            if not entries:
                show_message_screen(stdscr, t("browser.recent"), [t("browser.empty_recent")])
                continue
            action, entry = _pick_from_entries(
                stdscr,
                t("browser.recent"),
                entries,
                preamble=[t("browser.host", host=host_label)],
                extra_keys={"b": "manual"},
            )
            if action == "manual":
                entry = _ask_path(stdscr, host_key, host_label, is_remote)
            elif action != "picked":
                entry = None
            if entry is not None and is_remote:
                checked, error = _validate(host_key, True, entry.path)
                if error is not None or checked is None:
                    _show_error(stdscr, error or BrowseError("not_found", entry.path, host_key), host_label)
                    continue
                entry = checked
        elif pick.selected_key == "search":
            entry = _search(stdscr, host_key, host_label, is_remote, roots, catalog_cache)
        elif pick.selected_key == "tree":
            result = _tree(stdscr, host_key, host_label, is_remote, state_dir, roots, book, catalog_cache)
            if result.kind == "host":
                return None
            entry = result.entry
        elif pick.selected_key == "path":
            entry = _ask_path(stdscr, host_key, host_label, is_remote)
        if entry is None:
            continue
        if not multi:
            return [WorkspacePick(host_key, host_label, is_remote, entry)]
        if all(item.path != entry.path for item in basket):
            basket.append(entry)


def _pick_agent(stdscr, preamble: Sequence[str], *, remote: bool = False) -> Optional[str]:
    agents_cfg = load_config()["agents"]
    availability = {
        label: remote or resolve_agent_command(label, agents_cfg) is not None
        for label in CREATE_AGENT_ORDER
    }
    ordered = [label for label in CREATE_AGENT_ORDER if availability[label]]
    ordered.extend(label for label in CREATE_AGENT_ORDER if not availability[label])
    items = [
        (
            label,
            f'{label}  ·  {t("wizard.agent_remote_check" if remote else "wizard.agent_installed" if availability[label] else "wizard.agent_missing")}',
        )
        for label in ordered
    ]
    while True:
        pick = run_list_picker(
            stdscr,
            t("wizard.pick_agent"),
            items,
            footer_hint=t("wizard.hint_list"),
            preamble=list(preamble) + [t("wizard.agent_login_hint")],
        )
        if pick.cancelled or not pick.selected_key:
            return None
        if availability.get(pick.selected_key):
            return pick.selected_key
        show_message_screen(
            stdscr,
            t("wizard.agent_missing_title"),
            [t("wizard.agent_missing_action"), t("menu.cancel")],
        )


def _pick_role(stdscr, preamble: Sequence[str]) -> Optional[str]:
    items = [("__unassigned__", t("role.unassigned"))]
    items.extend((role, t(f"role.{role}")) for role in ROLE_IDS)
    pick = run_list_picker(
        stdscr,
        t("wizard.pick_role"),
        items + [("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=list(preamble),
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return None
    return "" if pick.selected_key == "__unassigned__" else pick.selected_key


def _pick_placement(
    stdscr,
    is_remote: bool,
    preamble: Sequence[str],
    bound_window: str,
    *,
    task_flow: bool = False,
) -> Optional[Tuple[str, str]]:
    labels = {
        "new": t("struct.place_group" if task_flow else "struct.place_window"),
        "current": t("struct.place_beside" if task_flow else "struct.place_current"),
        "bound": t("struct.place_current"),
        "pick": t("struct.place_pick"),
    }
    keys = ["new", "current"] if task_flow else placement_choices(is_remote)
    if bound_window and "current" in keys:
        keys = ["new", "bound", "pick"]
    items = [(key, labels[key]) for key in keys]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("struct.placement"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=list(preamble),
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return None
    return pick.selected_key, labels[pick.selected_key]


def _pick_layout(stdscr, preamble: Sequence[str], placement: str = "new") -> Optional[Tuple[str, str]]:
    items = [
        ("auto", t("browser.layout_auto")),
        ("horizontal", t("browser.layout_h")),
        ("vertical", t("browser.layout_v")),
    ]
    if placement == "new":
        items.extend([
            ("main-horizontal", t("browser.layout_main_h")),
            ("main-vertical", t("browser.layout_main_v")),
        ])
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("browser.layout"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=list(preamble),
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return None
    label = dict(items)[pick.selected_key]
    return pick.selected_key, label


def _path_preamble(picks: Sequence[WorkspacePick], extra: Sequence[str] = ()) -> List[str]:
    lines = [t("browser.host", host=picks[0].host_label)]
    for pick in picks:
        lines.append(t("browser.project_line", name=pick.entry.name, kind=kind_label(pick.entry.is_git)))
        lines.append(pick.entry.path)
    lines.extend(extra)
    return lines


def run_workspace_create(
    stdscr,
    tower,
    state_dir: Path,
    *,
    multi: bool,
    bound_window: str = "",
    open_tree: bool = False,
    task_flow: bool = False,
) -> None:
    """Create from the selected host, workspace, Agent, and optional task role."""

    if task_flow:
        _run_quick_start(stdscr, tower, state_dir)
        return

    picks = pick_workspaces(stdscr, tower, state_dir, multi=multi, open_tree=open_tree)
    if not picks:
        return
    preamble = _path_preamble(picks)
    agent = _pick_agent(stdscr, preamble, remote=picks[0].is_remote)
    if not agent:
        return
    placed = _pick_placement(
        stdscr, picks[0].is_remote, preamble + [agent], bound_window, task_flow=False,
    )
    if placed is None:
        return
    placement, placement_label = placed
    laid = _pick_layout(stdscr, preamble + [agent, placement_label], placement)
    if laid is None:
        return
    layout_choice, layout_label = laid
    confirm = run_list_picker(
        stdscr,
        t("browser.confirm_title"),
        [("create", t("browser.confirm_create")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=preamble + [agent, placement_label, layout_label],
    )
    if confirm.cancelled or confirm.selected_key != "create":
        return

    window_id = ""
    remote_session = ""
    if placement == "bound":
        window_id = bound_window
    elif placement == "current":
        if picks[0].is_remote:
            destination = _pick_remote_task_group(stdscr, tower, picks[0].host_label)
            if destination is None:
                return
            remote_session, window_id = destination
        else:
            selected = tower.visible_rows[tower.selected] if tower.visible_rows else {}
            if selected.get("kind") == "window":
                selected = next(
                    (row for row in tower.rows if row.get("pane_id") == selected.get("rename_pane_id")),
                    selected,
                )
            window_id = selected.get("window_id") or structure.current_window_id(tower.session)
    elif placement == "pick":
        window_id = _pick_window(stdscr, tower) or ""
        if not window_id:
            return
    plan = LaunchPlan(
        host_key=picks[0].host_key,
        host_label=picks[0].host_label,
        is_remote=picks[0].is_remote,
        workspaces=tuple(WorkspaceRef(pick.entry.name, pick.entry.path) for pick in picks),
        agents=(agent,),
        layout=WINDOW_LAYOUT.get(layout_choice, layout_choice),
        placement=placement,
        window_id=window_id,
        remote_session=remote_session,
        pane_direction=PANE_LAYOUT.get(layout_choice, layout_choice),
    )
    try:
        results = execute_launch_plan(
            plan,
            session=tower.session,
            state_dir=state_dir,
            overrides=tower.overrides,
        )
    except LaunchPlanError as exc:
        message = t("wizard.project_missing") if "작업공간을 확인할 수 없습니다" in str(exc) else t("wizard.launch_failed")
        show_message_screen(stdscr, t("browser.failed"), [message])
        return
    lines = []
    for result in results:
        if result.ok:
            lines.append(f"{result.target.project_name}  {result.target.agent_label}  ·  {t('wizard.started')}")
        else:
            lines.append(t("wizard.launch_failed"))
    show_message_screen(stdscr, t("wizard.result_title"), lines)


def _pick_task_environment(stdscr, tower) -> Optional[Tuple[str, str, bool]]:
    from ..state.environments import ssh_target_available
    from .settings_menu import open_environment_profiles

    while True:
        choices = [("local", t("environment.local"), False)]
        choices.extend(
            (row["alias"], row.get("display_name") or row.get("name") or row["alias"], True)
            for row in getattr(tower, "remote_hosts", []) if row.get("alias")
        )
        items = [(key, f"{label} · SSH" if remote else label) for key, label, remote in choices]
        items.extend([("add", t("environment.add")), ("cancel", t("menu.cancel"))])
        pick = run_list_picker(stdscr, t("environment.step1"), items, footer_hint=t("wizard.hint_list"))
        if pick.cancelled or pick.selected_key in (None, "cancel"):
            return None
        if pick.selected_key == "add":
            open_environment_profiles(stdscr, tower)
            continue
        selected = next((row for row in choices if row[0] == pick.selected_key), None)
        if selected is None:
            continue
        key, label, remote = selected
        if not remote:
            return tower.local_host, label, False
        while not ssh_target_available(key):
            retry = run_list_picker(
                stdscr, t("environment.offline"),
                [("retry", t("environment.retry")), ("other", t("environment.other"))],
                preamble=[t("environment.offline_message").format(name=label)],
                footer_hint=t("wizard.hint_list"),
            )
            if retry.cancelled or retry.selected_key != "retry":
                break
        else:
            return key, label, True


def _pick_task_project(
    stdscr, state_dir: Path, host_key: str, host_label: str, is_remote: bool,
) -> Optional[WorkspacePick]:
    current = None if is_remote else current_project_entry()
    recent_paths = load_recent(state_dir, host_key)
    while True:
        items = []
        if current is not None:
            items.append(("current", t("wizard.current_folder", name=current.name)))
        if recent_paths:
            items.append(("recent", t("environment.recent")))
        items.extend([("other", t("wizard.other_project")), ("cancel", t("menu.cancel"))])
        preamble = [host_label]
        pick = run_list_picker(
            stdscr, t("environment.step2"), items,
            footer_hint=t("wizard.hint_list"), preamble=preamble,
        )
        if pick.cancelled or pick.selected_key in (None, "cancel"):
            return None
        if pick.selected_key == "current" and current is not None:
            return WorkspacePick(host_key, host_label, is_remote, current)
        if pick.selected_key == "recent":
            entries = _recent_entries(host_key, host_label, is_remote, state_dir, stdscr) or []
            if entries:
                action, entry = _pick_from_entries(
                    stdscr, t("environment.recent"), entries, searchable=True,
                    preamble=[host_label],
                )
                if action == "picked" and entry is not None:
                    return WorkspacePick(host_key, host_label, is_remote, entry)
            else:
                show_message_screen(stdscr, t("environment.recent"), [t("browser.empty_recent")])
            continue
        if pick.selected_key == "other":
            roots = load_config()["project_roots"]
            result = _tree(stdscr, host_key, host_label, is_remote, state_dir, roots, TreeBook(), {})
            if result.kind == "entry" and result.entry is not None:
                return WorkspacePick(host_key, host_label, is_remote, result.entry)
            if result.kind == "host":
                return None


def _available_remote_agents(host: str, agents_cfg: dict) -> Optional[set]:
    checks = []
    for index, label in enumerate(QUICK_AGENT_ORDER):
        try:
            binary = shlex.split(agents_cfg.get(label) or "")[0]
        except (ValueError, IndexError):
            continue
        checks.append((index, label, binary))
    script = "\n".join([
        *(f"command -v {shlex.quote(binary)} >/dev/null 2>&1 && printf '{index}\\n'"
          for index, _label, binary in checks),
        "true",
    ])
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", host, script],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    available = {int(value) for value in (result.stdout or "").splitlines() if value.isdecimal()}
    return {label for index, label, _binary in checks if index in available}


def _run_quick_start(stdscr, tower, state_dir: Path) -> None:
    environment = _pick_task_environment(stdscr, tower)
    if environment is None:
        return
    host_key, host_label, is_remote = environment
    pick = _pick_task_project(stdscr, state_dir, host_key, host_label, is_remote)
    if pick is None:
        return

    agents_cfg = load_config()["agents"]
    if is_remote:
        available = _available_remote_agents(host_key, agents_cfg)
        if available is None:
            show_message_screen(
                stdscr, t("environment.offline"),
                [t("environment.offline_message").format(name=host_label)],
            )
            return
    else:
        available = {name for name in QUICK_AGENT_ORDER if resolve_agent_command(name, agents_cfg)}
    items = [(name, name) for name in QUICK_AGENT_ORDER if name in available]
    if not items:
        show_message_screen(stdscr, t("environment.step3"), [t("wizard.no_agents_available")])
        return
    chosen = run_list_picker(
        stdscr, t("environment.step3"), items,
        preamble=[pick.entry.name], footer_hint=t("wizard.hint_list"),
    )
    if chosen.cancelled or not chosen.selected_key:
        return
    agent = str(chosen.selected_key)
    task_name = f"{pick.entry.name} · {agent}"
    plan = LaunchPlan(
        host_key=pick.host_key,
        host_label=pick.host_label if is_remote else tower.local_host,
        is_remote=is_remote,
        workspaces=(WorkspaceRef(pick.entry.name, pick.entry.path),),
        agents=(agent,),
        layout=WINDOW_LAYOUT["auto"],
        placement="new",
        pane_direction="auto",
    )
    try:
        results = execute_launch_plan(
            plan, session=tower.session, state_dir=state_dir,
            agents_cfg=agents_cfg, overrides=tower.overrides,
        )
    except LaunchPlanError:
        show_message_screen(stdscr, t("browser.failed"), [t("wizard.launch_failed")])
        return
    for result in results:
        if not result.ok or not result.pane_id or not result.pane_pid or not result.session:
            continue
        pane_key = f"{host_key}:{result.pane_id}" if is_remote else result.pane_id
        tower.overrides.drop_if_stale(pane_key, result.session, result.pane_pid)
        tower.overrides.set_task_name(pane_key, task_name, result.session, result.pane_pid)
        tower.overrides.clear_field(pane_key, "role")
    tower.load()
    lines = [
        f'{result.target.project_name} · {agent}  ·  '
        f'{t("wizard.started") if result.ok else t("wizard.launch_failed")}'
        for result in results
    ]
    show_message_screen(stdscr, t("wizard.result_title"), lines or [t("wizard.launch_failed")])


def current_project_entry(path: Optional[Path] = None) -> Optional[ProjectEntry]:
    """Recommend the current folder only when it clearly looks like a project."""

    try:
        project = (path or Path.cwd()).resolve()
        if not project.is_dir():
            return None
        markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Gemfile")
        if not any((project / marker).exists() for marker in markers):
            return None
        return ProjectEntry(project.name or str(project), str(project), (project / ".git").exists())
    except OSError:
        return None


def _pick_window(stdscr, tower) -> Optional[str]:
    windows = structure.list_windows(tower.session)
    items = []
    for item in windows:
        index = str(item.get("window_index") or "")
        position = str(int(index) + 1) if index.isdecimal() else index
        items.append((item["window_id"], f'{position}. {item["window_name"]}'))
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(stdscr, t("struct.place_pick"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return None
    return pick.selected_key


def _pick_remote_task_group(stdscr, tower, host_label: str) -> Optional[Tuple[str, str]]:
    groups = {}
    for row in tower.rows:
        if not row.get("remote") or row.get("host") != host_label:
            continue
        session, window_id = str(row.get("session") or ""), str(row.get("window_id") or "")
        if not session or not structure.valid_window_id(window_id):
            continue
        key = (session, window_id)
        groups.setdefault(key, []).append(row.get("task_name") or row.get("project") or "")
    if not groups:
        show_message_screen(stdscr, t("struct.failed"), [t("struct.no_destination")])
        return None
    items = []
    for key, names in groups.items():
        label = " · ".join(dict.fromkeys(name for name in names if name)) or host_label
        items.append((key, label))
    pick = run_list_picker(stdscr, t("struct.move_title"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key not in groups:
        return None
    return pick.selected_key
