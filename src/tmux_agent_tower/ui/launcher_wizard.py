"""Workspace Launcher screen flow (the "N" / "W" keys in the main Tower).

This module only ever *reads* project directories and the process table,
and creates NEW tmux panes via ``launcher/spawn.py``. It never touches an
existing pane -- see ``launcher/spawn.py``'s module docstring for the
enforced rules.
"""

from __future__ import annotations

import curses
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER
from ..launcher.discovery import (
    load_recent,
    manual_path_entry,
    ProjectEntry,
)
from .widgets import is_enter, is_escape, matches_letter, prompt_text, read_key, run_list_picker, safe_add, show_message_screen

LAYOUT_CYCLE = ["tiled", "even-horizontal", "even-vertical"]


def _check_remote_reachable(host_alias: str, timeout: float = 4.0) -> bool:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}", host_alias, "true"],
            capture_output=True,
            timeout=timeout + 1.0,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_remote_path_exists(host_alias: str, path: str, timeout: float = 4.0) -> bool:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}", host_alias, f"test -d {path!r}"],
            capture_output=True,
            timeout=timeout + 1.0,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _pick_host(stdscr, tower) -> Optional[Tuple[str, str, bool]]:
    """Returns ``(host_key, display_label, is_remote)`` or None if cancelled."""

    hosts: List[Tuple[str, str]] = [(tower.local_host, tower.local_host)]
    hosts += [(h["alias"], h["name"]) for h in tower.remote_hosts]

    if len(hosts) == 1:
        return hosts[0][0], hosts[0][1], False

    pick = run_list_picker(stdscr, t("wizard.pick_host"), hosts, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key is None:
        return None

    label = dict(hosts)[pick.selected_key]
    return pick.selected_key, label, pick.selected_key != tower.local_host


def _pick_projects(
    stdscr,
    multi: bool,
    state_dir: Path,
    host_key: str,
    discovered: List[ProjectEntry],
    validate_manual_path: Callable[[str], bool],
    no_roots_hint: Optional[str] = None,
) -> Optional[List[ProjectEntry]]:
    """Shared local/remote project picker.

    ``discovered`` is already resolved (git-repo scan results, local or
    remote). ``validate_manual_path`` checks a manually-typed path exists
    (local: ``Path.is_dir()``; remote: an SSH ``test -d``) -- the picker
    itself doesn't know or care which.
    """

    recent_paths = load_recent(state_dir, host_key)
    by_path = {p.path: p for p in discovered}

    items: List[Tuple[str, str]] = []
    seen = set()
    path_to_entry = dict(by_path)

    for path in recent_paths:
        entry = by_path.get(path)
        if entry is None:
            continue  # Don't re-validate every recent path on every open; stale entries just drop off.
        items.append((entry.path, t("wizard.recent_prefix") + entry.name))
        seen.add(entry.path)

    for entry in discovered:
        if entry.path not in seen:
            items.append((entry.path, entry.name))

    if not items and no_roots_hint:
        show_message_screen(stdscr, t("wizard.pick_project_multi" if multi else "wizard.pick_project_single"), [no_roots_hint])

    title = t("wizard.pick_project_multi") if multi else t("wizard.pick_project_single")
    hint = t("wizard.hint_multi") if multi else t("wizard.hint_single_search")

    while True:
        pick = run_list_picker(
            stdscr, title, items, multi=multi, searchable=True,
            extra_keys={"b": "manual_path"}, footer_hint=hint,
        )

        if pick.cancelled:
            return None

        if pick.extra == "manual_path":
            path_str = prompt_text(stdscr, t("wizard.manual_path_prompt"))
            if path_str is None or not path_str.strip():
                continue
            candidate = path_str.strip()
            if not validate_manual_path(candidate):
                show_message_screen(stdscr, t("wizard.manual_path_not_found"), [candidate])
                continue
            entry = manual_path_entry(candidate)
            if multi:
                items.append((entry.path, entry.name))
                path_to_entry[entry.path] = entry
                continue
            return [entry]

        if multi:
            selected_paths = pick.selected_keys
            if not selected_paths:
                show_message_screen(stdscr, t("wizard.no_projects_selected"), [])
                continue
            return [path_to_entry.get(p) or manual_path_entry(p) for p in selected_paths]

        if pick.selected_key is None:
            continue
        return [path_to_entry.get(pick.selected_key) or manual_path_entry(pick.selected_key)]


def _pick_agent(stdscr) -> Optional[str]:
    items = [(label, label) for label in AGENT_LAUNCH_ORDER]
    pick = run_list_picker(stdscr, t("wizard.pick_agent"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled:
        return None
    return pick.selected_key


def _run_preview(stdscr, host_label: str, rows: List[List[str]]) -> Optional[Tuple[List[List[str]], str]]:
    """``rows`` are ``[project_name, agent_label]`` pairs, mutated in place
    on agent override. Returns ``(rows, layout)`` on confirm, else None.
    """

    selected_index = 0
    layout_index = 0
    stdscr.timeout(-1)

    try:
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            layout = LAYOUT_CYCLE[layout_index]

            safe_add(stdscr, 0, 2, t("wizard.preview_title"), curses.A_BOLD)
            safe_add(stdscr, 2, 2, t("wizard.preview_host_label", host=host_label))
            safe_add(stdscr, 3, 2, t("wizard.preview_layout_label", layout=t(f"wizard.layout.{layout}")))

            list_top = 5
            for i, (project_name, agent_label) in enumerate(rows):
                y = list_top + i
                is_selected = i == selected_index
                attr = curses.A_REVERSE if is_selected else 0
                marker = "> " if is_selected else "  "
                safe_add(stdscr, y, 2, f"{marker}{project_name:<24} → {agent_label}", attr)

            safe_add(stdscr, height - 2, 2, t("wizard.hint_preview"), curses.A_DIM)
            stdscr.refresh()

            key = read_key(stdscr)

            if is_escape(key):
                return None
            if key == curses.KEY_UP:
                selected_index = max(0, selected_index - 1)
                continue
            if key == curses.KEY_DOWN:
                selected_index = min(len(rows) - 1, selected_index + 1)
                continue
            if is_enter(key):
                return rows, layout
            if matches_letter(key, "l"):
                layout_index = (layout_index + 1) % len(LAYOUT_CYCLE)
                continue
            if matches_letter(key, "a") and rows:
                new_agent = _pick_agent(stdscr)
                if new_agent:
                    rows[selected_index][1] = new_agent
                continue
    finally:
        stdscr.timeout(200)


def run_launcher(stdscr, tower, multi: bool, state_dir: Path) -> None:
    """N and W both start by choosing a host, then a workspace path."""

    from .workspace_browser import run_workspace_create

    run_workspace_create(stdscr, tower, state_dir, multi=multi)
