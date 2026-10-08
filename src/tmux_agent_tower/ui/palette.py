"""Small search palette backed by Tower rows and existing UI routes."""

from __future__ import annotations

import curses

from ..i18n import t
from .render import truncate_to_width
from .widgets import is_backspace, is_enter, is_escape, read_key, safe_add


def _commands():
    return [
        ("new_task", t("palette.new_task")),
        ("add_ssh", t("palette.add_ssh")),
        ("environments", t("palette.environments")),
        ("live", t("palette.live")),
        ("saved", t("palette.saved")),
        ("template", t("palette.template")),
        ("settings", t("palette.settings")),
        ("workflow", t("palette.workflow")),
    ]


def _rows(tower):
    found = {}
    for row in [*getattr(tower, "visible_rows", []), *getattr(tower, "rows", [])]:
        kind = row.get("kind")
        if kind in {"pane", "work_group", "work_group_stale"}:
            category = "work"
        elif kind in {"folder", "other_section"}:
            category = "folder"
        elif kind in {"window", "window_asset"}:
            category = "window"
        else:
            continue
        key = str(row.get("key") or row.get("window_ref") or row.get("folder_id") or "")
        if not key:
            continue
        label = str(
            row.get("display_name") or row.get("task_name") or row.get("project")
            or row.get("folder_name") or row.get("window_display_name") or key
        )
        search = " ".join(str(row.get(field) or "") for field in (
            "display_name", "task_name", "project", "agent", "folder_name",
            "window_display_name", "work_group_name", "window_name",
        ))
        found[(kind, key)] = (category, label, search, row)
    return list(found.values())


def _items(tower, query: str):
    needle = query.casefold().strip()
    items = []
    for action, label in _commands():
        if not needle or needle in label.casefold():
            items.append(("command", action, label, None))
    for category, label, search, row in _rows(tower):
        corpus = f"{label} {search} {category}".casefold()
        if not needle or needle in corpus:
            items.append((category, str(row.get("key") or row.get("window_ref") or row.get("folder_id") or ""), label, row))
    return items


def open_palette(stdscr, tower) -> None:
    query = ""
    selected = 0
    old_timeout = -1
    stdscr.timeout(old_timeout)
    try:
        while True:
            items = _items(tower, query)
            selected = min(selected, max(0, len(items) - 1))
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            safe_add(stdscr, 0, 2, t("palette.title"), curses.A_BOLD)
            safe_add(stdscr, 2, 2, f"/ {query}_", curses.A_BOLD)
            top = max(0, selected - max(1, height - 6) + 1)
            for index, (category, _key, label, _row) in enumerate(items[top:top + max(1, height - 5)], start=top):
                marker = "> " if index == selected else "  "
                line = f'{marker}{t("palette.category." + category)} · {label}'
                safe_add(stdscr, 4 + index - top, 0, truncate_to_width(line, max(0, width - 1)),
                         curses.A_REVERSE if index == selected else 0)
            if not items:
                safe_add(stdscr, 4, 2, t("wizard.no_matches"), curses.A_DIM)
            safe_add(stdscr, height - 2, 2, t("wizard.hint_list"), curses.A_DIM)
            stdscr.refresh()
            key = read_key(stdscr)
            if is_escape(key):
                return
            if key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key == curses.KEY_DOWN:
                selected = min(max(0, len(items) - 1), selected + 1)
            elif is_backspace(key):
                query = query[:-1]
                selected = 0
            elif is_enter(key):
                if items:
                    _activate(stdscr, tower, items[selected])
                return
            elif isinstance(key, str) and key.isprintable():
                query += key
                selected = 0
    finally:
        stdscr.timeout(200)


def _activate(stdscr, tower, item) -> None:
    category, key, _label, row = item
    if category != "command":
        search = next((str(row.get(field) or "") for field in (
            "task_name", "project", "agent", "folder_name", "window_display_name",
            "work_group_name",
        ) if row.get(field)), _label)
        tower.set_filter(search)
        for index, visible in enumerate(tower.visible_rows):
            if str(visible.get("key") or visible.get("window_ref") or visible.get("folder_id") or "") == key:
                tower.selected = index
                break
        from .tower import _open_selected_row

        _open_selected_row(stdscr, tower)
        tower.clear_filter()
        return

    if key == "new_task":
        from .structure_menu import start_task

        start_task(stdscr, tower)
    elif key == "add_ssh":
        from .settings_menu import open_environment_profiles

        open_environment_profiles(stdscr, tower, start_add=True)
    elif key == "environments":
        from .settings_menu import open_environment_profiles

        open_environment_profiles(stdscr, tower)
    elif key == "live":
        from .live_view import open_live_view

        open_live_view(stdscr, tower)
    elif key == "saved":
        from .tower import STATE_DIR
        from .worksets import open_saved_collection

        open_saved_collection(stdscr, tower, STATE_DIR)
    elif key == "template":
        from .tower import STATE_DIR
        from .worksets import open_work_templates

        open_work_templates(stdscr, tower, STATE_DIR)
    elif key == "settings":
        from .settings_menu import open_settings

        open_settings(stdscr, tower)
    elif key == "workflow":
        from .tower import STATE_DIR
        from .workflow_presets import open_workflow_presets

        open_workflow_presets(stdscr, tower, STATE_DIR)
