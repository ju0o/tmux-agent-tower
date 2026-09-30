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
from typing import List, Optional, Tuple

from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER, load_config
from ..launcher.discovery import find_git_projects, load_recent, record_recent, manual_path_entry, ProjectEntry
from ..launcher.spawn import SpawnTarget, spawn_local, spawn_remote
from .widgets import ENTER_KEYS, ESC, prompt_text, run_list_picker, safe_add, show_message_screen

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


def _pick_local_projects(stdscr, multi: bool, state_dir: Path) -> Optional[List[ProjectEntry]]:
    cfg = load_config()
    roots = cfg["project_roots"]
    recent_paths = load_recent(state_dir)
    discovered = find_git_projects(roots) if roots else []
    by_path = {p.path: p for p in discovered}

    items: List[Tuple[str, str]] = []
    seen = set()

    for path in recent_paths:
        entry = by_path.get(path) or (manual_path_entry(path) if Path(path).is_dir() else None)
        if entry is None:
            continue
        items.append((entry.path, t("wizard.recent_prefix") + entry.name))
        seen.add(entry.path)

    for entry in discovered:
        if entry.path not in seen:
            items.append((entry.path, entry.name))

    path_to_entry = {p.path: p for p in discovered}
    for path in recent_paths:
        if path in by_path:
            path_to_entry[path] = by_path[path]

    if not items and not roots:
        show_message_screen(stdscr, t("wizard.pick_project_multi" if multi else "wizard.pick_project_single"), [t("wizard.no_project_roots")])

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
            candidate = Path(path_str).expanduser()
            if not candidate.is_dir():
                show_message_screen(stdscr, t("wizard.manual_path_not_found"), [str(candidate)])
                continue
            entry = manual_path_entry(str(candidate))
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


def _pick_remote_projects(stdscr, host_alias: str, multi: bool) -> Optional[List[ProjectEntry]]:
    entries: List[ProjectEntry] = []

    while True:
        hint = t("wizard.manual_path_done_hint") if (multi and entries) else ""
        path_str = prompt_text(stdscr, t("wizard.manual_path_prompt") + hint)

        if path_str is None:
            return entries if entries else None

        if not path_str.strip():
            break

        if not _check_remote_path_exists(host_alias, path_str.strip()):
            show_message_screen(stdscr, t("wizard.manual_path_not_found"), [path_str])
            continue

        entries.append(manual_path_entry(path_str.strip()))

        if not multi:
            break

    return entries or None


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

            key = stdscr.getch()
            char = chr(key).lower() if 0 <= key < 256 else ""

            if key == ESC:
                return None
            if key == curses.KEY_UP:
                selected_index = max(0, selected_index - 1)
                continue
            if key == curses.KEY_DOWN:
                selected_index = min(len(rows) - 1, selected_index + 1)
                continue
            if key in ENTER_KEYS:
                return rows, layout
            if char == "l":
                layout_index = (layout_index + 1) % len(LAYOUT_CYCLE)
                continue
            if char == "a" and rows:
                new_agent = _pick_agent(stdscr)
                if new_agent:
                    rows[selected_index][1] = new_agent
                continue
    finally:
        stdscr.timeout(200)


def run_launcher(stdscr, tower, multi: bool, state_dir: Path) -> None:
    host_pick = _pick_host(stdscr, tower)
    if host_pick is None:
        return
    host_key, host_label, is_remote = host_pick

    if is_remote:
        if not _check_remote_reachable(host_key):
            show_message_screen(stdscr, t("wizard.remote_unreachable", host=host_label), [])
            return
        projects = _pick_remote_projects(stdscr, host_key, multi)
    else:
        projects = _pick_local_projects(stdscr, multi, state_dir)

    if not projects:
        return

    default_agent = _pick_agent(stdscr)
    if default_agent is None:
        return

    rows: List[List[str]] = [[p.name, default_agent] for p in projects]
    result = _run_preview(stdscr, host_label, rows)
    if result is None:
        return
    final_rows, layout = result

    cfg = load_config()
    targets = [
        SpawnTarget(project_path=p.path, project_name=p.name, agent_label=agent)
        for p, (_, agent) in zip(projects, final_rows)
    ]

    if is_remote:
        results = spawn_remote(host_key, host_label, targets, cfg["agents"], layout=layout)
    else:
        results = spawn_local(tower.session, host_label, targets, cfg["agents"], layout=layout)
        for p in projects:
            record_recent(state_dir, p.path)

    result_lines = []
    for r in results:
        if r.ok:
            result_lines.append(f"{r.target.project_name} {t('wizard.result_ok_suffix', agent=r.target.agent_label)}")
        else:
            result_lines.append(f"{r.target.project_name} {t('wizard.result_fail_suffix', detail=r.detail)}")

    show_message_screen(stdscr, t("wizard.result_title"), result_lines)
