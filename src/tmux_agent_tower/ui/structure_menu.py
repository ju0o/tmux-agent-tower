"""Local tmux structure: create, move, rename, layout, and close.

Phone remote does not call this module. Every write goes through
``tmux.structure``, which targets a window id or a pane id. Names are
labels. Destructive actions ask first, and cancel is the default.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER, load_config
from ..launcher.discovery import find_git_projects
from ..launcher.spawn import SpawnTarget, spawn_into_window, spawn_local
from ..state.bindings import ProjectBindingStore
from ..tmux import structure
from .launcher_wizard import _pick_agent, _pick_projects
from .widgets import prompt_text, run_list_picker, show_message_screen


def window_close_summary(window_name: str, window_id: str, panes: List[dict]) -> List[str]:
    """What the close screen shows before the user confirms."""

    lines = [
        f"{window_name or '-'}  {window_id}",
        t("struct.window_panes").format(n=len(panes)),
    ]
    for pane in panes:
        attention = pane.get("attention") or "none"
        extra = ""
        if attention == "approval_required":
            extra = "  " + t("state.approval")
        elif attention == "input_required":
            extra = "  " + t("state.input")
        lines.append(
            f"{pane.get('pane_id') or '-'}  {pane.get('agent') or '-'}  {pane.get('status') or '-'}{extra}"
        )
    return lines


def run_zero_action(stdscr, tower, action: str) -> None:
    if action == "task":
        start_task(stdscr, tower)
    elif action == "window":
        create_empty_window(stdscr, tower)
    elif action == "pane":
        add_pane(stdscr, tower, structure.current_window_id(tower.session))
    elif action == "workspace":
        from .tower import STATE_DIR
        from .launcher_wizard import run_launcher

        run_launcher(stdscr, tower, multi=True, state_dir=STATE_DIR)


def open_create_hub(stdscr, tower) -> None:
    pick = run_list_picker(
        stdscr,
        t("struct.create_title"),
        [
            ("task", t("struct.create_task")),
            ("window", t("struct.create_window")),
            ("pane", t("struct.create_pane")),
            ("workspace", t("struct.create_workspace")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "task":
        start_task(stdscr, tower)
    elif pick.selected_key == "window":
        create_empty_window(stdscr, tower)
    elif pick.selected_key == "pane":
        add_pane(stdscr, tower, _preferred_window(tower))
    elif pick.selected_key == "workspace":
        run_zero_action(stdscr, tower, "workspace")


def open_window_control(stdscr, tower, window_id: str) -> None:
    """Enter on a window. Does not select that window in tmux."""

    if not structure.valid_window_id(window_id):
        show_message_screen(stdscr, t("struct.failed"), [t("nav.stale")])
        return
    while True:
        meta = _window_meta(tower, window_id)
        pick = run_list_picker(
            stdscr,
            t("struct.window_title"),
            [
                ("add", t("struct.add_pane")),
                ("new", t("struct.create_window")),
                ("rename", t("struct.rename")),
                ("layout", t("struct.layout")),
                ("panes", t("struct.manage_panes")),
                ("close", t("struct.close_window")),
                ("cancel", t("menu.cancel")),
            ],
            footer_hint=t("wizard.hint_list"),
            preamble=[f'{meta.get("window_name") or "-"}  {window_id}', t("struct.window_panes").format(n=meta.get("pane_count") or 0)],
        )
        if pick.cancelled or pick.selected_key in (None, "cancel"):
            return
        if pick.selected_key == "add":
            add_pane(stdscr, tower, window_id)
        elif pick.selected_key == "new":
            create_empty_window(stdscr, tower)
        elif pick.selected_key == "rename":
            rename_window(stdscr, tower, window_id)
        elif pick.selected_key == "layout":
            choose_layout(stdscr, tower, window_id)
        elif pick.selected_key == "panes":
            if _manage_panes(stdscr, tower, window_id) == "gone":
                return
        elif pick.selected_key == "close":
            if close_window(stdscr, tower, window_id):
                return
        tower.load()


def open_context_menu(stdscr, tower) -> None:
    if not tower.visible_rows:
        open_create_hub(stdscr, tower)
        return
    row = tower.visible_rows[tower.selected]
    kind = row.get("kind")
    if kind == "window":
        _window_menu(stdscr, tower, row)
    elif kind == "zero":
        run_zero_action(stdscr, tower, row.get("action") or "")
    else:
        open_pane_menu(stdscr, tower, row)


def open_pane_menu(stdscr, tower, row: dict) -> str:
    """Pane actions. Returns ``closed`` when the pane was killed."""

    if row.get("remote") or not str(row.get("pane_id") or "").startswith("%"):
        show_message_screen(stdscr, t("control.remote_unsupported"), [])
        return ""
    pick = run_list_picker(
        stdscr,
        t("struct.pane_menu"),
        [
            ("control", t("struct.act_control")),
            ("prompt", t("struct.act_prompt")),
            ("edit", t("struct.act_edit")),
            ("move", t("struct.act_move")),
            ("break", t("struct.act_break")),
            ("close", t("struct.act_close")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
        preamble=[f'{row.get("project") or "-"} / {row.get("agent") or "-"}', row.get("pane_id") or ""],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return ""
    if pick.selected_key == "control":
        from .control_view import open_control_view

        open_control_view(stdscr, tower, row.get("key"))
    elif pick.selected_key == "prompt":
        from .control_view import _prompt

        _prompt(stdscr, tower, row)
    elif pick.selected_key == "edit":
        tower.edit_selected(stdscr)
    elif pick.selected_key == "move":
        move_pane(stdscr, tower, row)
    elif pick.selected_key == "break":
        break_pane(stdscr, tower, row)
    elif pick.selected_key == "close":
        from .control_view import _close

        if _close(stdscr, tower, row):
            return "closed"
    return ""


def start_task(stdscr, tower) -> None:
    """One project and one agent, then where the new pane should live."""

    cfg = load_config()
    roots = cfg["project_roots"]
    discovered = find_git_projects(roots) if roots else []
    projects = _pick_projects(
        stdscr,
        False,
        _state_dir(),
        tower.local_host,
        discovered,
        lambda path: Path(path).expanduser().is_dir(),
        t("wizard.no_projects_found") if not discovered else None,
    )
    if not projects:
        return
    agent = _pick_agent(stdscr)
    if not agent:
        return
    place = run_list_picker(
        stdscr,
        t("struct.placement"),
        [
            ("new", t("struct.place_window")),
            ("current", t("struct.place_current")),
            ("pick", t("struct.place_pick")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if place.cancelled or place.selected_key in (None, "cancel"):
        return
    project = projects[0]
    target = SpawnTarget(project.path, project.name, agent)
    bindings = ProjectBindingStore(_state_dir() / "project-bindings.json")
    if place.selected_key == "new":
        spawn_local(
            tower.session,
            [target],
            cfg["agents"],
            bindings=bindings,
            overrides=tower.overrides,
        )
        return
    window_id = structure.current_window_id(tower.session) if place.selected_key == "current" else _pick_window(stdscr, tower)
    if not window_id:
        show_message_screen(stdscr, t("struct.failed"), [t("nav.stale")])
        return
    spawn_into_window(
        tower.session,
        window_id,
        target,
        cfg["agents"],
        bindings=bindings,
        overrides=tower.overrides,
    )


def create_empty_window(stdscr, tower) -> None:
    name = prompt_text(stdscr, t("struct.name_prompt"), context_lines=[t("struct.name_optional")])
    if name is None:
        return
    how = run_list_picker(
        stdscr,
        t("struct.create_window"),
        [
            ("shell", t("struct.empty_shell")),
            ("project", t("struct.with_project")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if how.cancelled or how.selected_key in (None, "cancel"):
        return
    cwd = ""
    agent = ""
    project_name = ""
    project_path = ""
    if how.selected_key == "project":
        cfg = load_config()
        roots = cfg["project_roots"]
        discovered = find_git_projects(roots) if roots else []
        projects = _pick_projects(
            stdscr,
            False,
            _state_dir(),
            tower.local_host,
            discovered,
            lambda path: Path(path).expanduser().is_dir(),
            None,
        )
        if not projects:
            return
        project_path = projects[0].path
        project_name = projects[0].name
        cwd = project_path
        agent_pick = run_list_picker(
            stdscr,
            t("wizard.pick_agent"),
            [("shell", t("struct.empty_shell"))] + [(label, label) for label in AGENT_LAUNCH_ORDER] + [("cancel", t("menu.cancel"))],
            footer_hint=t("wizard.hint_list"),
        )
        if agent_pick.cancelled or agent_pick.selected_key in (None, "cancel"):
            return
        if agent_pick.selected_key != "shell":
            agent = agent_pick.selected_key
    created = structure.create_window(tower.session, name=name, cwd=cwd)
    if not created.ok:
        show_message_screen(stdscr, t("struct.failed"), [created.detail])
        return
    if agent and project_path:
        cfg = load_config()
        target = SpawnTarget(project_path, project_name, agent)
        from ..launcher.spawn import _finish_new_pane
        from ..launcher.config import resolve_agent_command

        command = resolve_agent_command(agent, cfg["agents"])
        _finish_new_pane(
            tower.session,
            target,
            command,
            created.pane_id,
            cfg["agents"],
            ProjectBindingStore(_state_dir() / "project-bindings.json"),
            tower.overrides,
        )
    show_message_screen(stdscr, t("struct.created"), [f"{created.window_id}  {created.pane_id}"])


def add_pane(stdscr, tower, window_id: str) -> None:
    if not structure.valid_window_id(window_id):
        window_id = _pick_window(stdscr, tower) or ""
    if not structure.valid_window_id(window_id):
        show_message_screen(stdscr, t("struct.failed"), [t("nav.stale")])
        return
    direction = run_list_picker(
        stdscr,
        t("struct.split"),
        [
            ("auto", t("struct.split_auto")),
            ("horizontal", t("struct.split_h")),
            ("vertical", t("struct.split_v")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
        preamble=[window_id],
    )
    if direction.cancelled or direction.selected_key in (None, "cancel"):
        return
    how = run_list_picker(
        stdscr,
        t("struct.create_pane"),
        [
            ("shell", t("struct.empty_shell")),
            ("project", t("struct.with_project")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
    )
    if how.cancelled or how.selected_key in (None, "cancel"):
        return
    if how.selected_key == "shell":
        created = structure.create_pane(window_id, direction=direction.selected_key)
        if not created.ok:
            show_message_screen(stdscr, t("struct.failed"), [created.detail])
            return
        show_message_screen(stdscr, t("struct.created"), [f"{created.window_id}  {created.pane_id}"])
        return
    cfg = load_config()
    roots = cfg["project_roots"]
    discovered = find_git_projects(roots) if roots else []
    projects = _pick_projects(
        stdscr,
        False,
        _state_dir(),
        tower.local_host,
        discovered,
        lambda path: Path(path).expanduser().is_dir(),
        None,
    )
    if not projects:
        return
    agent = _pick_agent(stdscr)
    if not agent:
        return
    result = spawn_into_window(
        tower.session,
        window_id,
        SpawnTarget(projects[0].path, projects[0].name, agent),
        cfg["agents"],
        bindings=ProjectBindingStore(_state_dir() / "project-bindings.json"),
        overrides=tower.overrides,
        direction=direction.selected_key,
    )
    show_message_screen(
        stdscr,
        t("struct.created") if result.ok else t("struct.failed"),
        [result.detail],
    )


def choose_layout(stdscr, tower, window_id: str) -> None:
    items = [
        (name, f'{t("struct.layout." + name)}   {structure.LAYOUT_PREVIEW[name]}')
        for name in structure.LAYOUTS
    ]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("struct.layout"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=[window_id],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    result = structure.apply_layout(tower.session, window_id, pick.selected_key)
    if not result.ok:
        show_message_screen(stdscr, t("struct.failed"), [result.detail])


def rename_window(stdscr, tower, window_id: str) -> None:
    meta = _window_meta(tower, window_id)
    name = prompt_text(
        stdscr,
        t("struct.rename_window"),
        initial=meta.get("window_name") or "",
        context_lines=[window_id, t("struct.name_is_label")],
    )
    if not name:
        return
    result = structure.rename_window(tower.session, window_id, name)
    if not result.ok:
        show_message_screen(stdscr, t("struct.failed"), [result.detail])


def move_pane(stdscr, tower, row: dict) -> None:
    pane_id = row.get("pane_id") or ""
    source = row.get("window_id") or structure.pane_window_id(pane_id)
    windows = [item for item in structure.list_windows(tower.session) if item["window_id"] != source]
    items = [(item["window_id"], f'{item["window_id"]}  {item["window_name"]}') for item in windows]
    items.append(("__new__", t("struct.act_break")))
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(
        stdscr,
        t("struct.move_title"),
        items,
        footer_hint=t("wizard.hint_list"),
        preamble=[pane_id],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "__new__":
        break_pane(stdscr, tower, row)
        return
    if not _confirm(stdscr, t("struct.move_title"), [f'{pane_id} → {pick.selected_key}']):
        return
    result = structure.move_pane(tower.session, pane_id, pick.selected_key)
    if not result.ok:
        show_message_screen(stdscr, t("struct.failed"), [t("struct." + result.detail) if result.detail in ("already_there", "not_found") else result.detail])


def break_pane(stdscr, tower, row: dict) -> None:
    pane_id = row.get("pane_id") or ""
    if not _confirm(stdscr, t("struct.break_title"), [pane_id, t("struct.break_hint")]):
        return
    result = structure.break_pane(tower.session, pane_id)
    if not result.ok:
        key = "struct." + result.detail
        show_message_screen(stdscr, t("struct.failed"), [t(key) if key != t(key) else result.detail])
        return
    show_message_screen(stdscr, t("struct.created"), [f"{result.window_id}  {result.pane_id}"])


def close_window(stdscr, tower, window_id: str) -> bool:
    """True when the window was closed."""

    windows = structure.list_windows(tower.session)
    inside = structure.panes_of_window(window_id)
    reason = structure.window_close_block(inside, tower.own_pane_id, len(windows))
    if reason == "tower_pane":
        show_message_screen(stdscr, t("struct.tower_window"), [])
        return False
    if reason == "last_window":
        show_message_screen(stdscr, t("struct.last_window"), [])
        return False
    if reason:
        show_message_screen(stdscr, t("struct.failed"), [reason])
        return False
    meta = _window_meta(tower, window_id)
    panes = [row for row in tower.rows if row.get("window_id") == window_id and row.get("pane_id") and not row.get("remote")]
    # Include Tower's own pane id if it lives here even though the list hides it.
    known = {row.get("pane_id") for row in panes}
    for pane_id in inside:
        if pane_id not in known:
            panes.append({"pane_id": pane_id, "agent": "-", "status": "-", "attention": "none"})
    pick = run_list_picker(
        stdscr,
        t("struct.close_window_title"),
        [("cancel", t("control.cancel")), ("close", t("struct.close_window_yes"))],
        footer_hint=t("wizard.hint_list"),
        preamble=window_close_summary(meta.get("window_name") or "", window_id, panes),
    )
    if pick.cancelled or pick.selected_key != "close":
        return False
    result = structure.kill_window(tower.session, window_id, tower.own_pane_id)
    if not result.ok:
        label = {
            "tower_pane": t("struct.tower_window"),
            "last_window": t("struct.last_window"),
            "not_found": t("nav.stale"),
        }.get(result.detail, result.detail)
        show_message_screen(stdscr, t("struct.failed"), [label])
        return False
    return True


def _window_menu(stdscr, tower, row: dict) -> None:
    window_id = row.get("window_id") or ""
    pick = run_list_picker(
        stdscr,
        t("struct.window_title"),
        [
            ("add", t("struct.add_pane")),
            ("rename", t("struct.rename")),
            ("layout", t("struct.layout")),
            ("close", t("struct.close_window")),
            ("cancel", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
        preamble=[row.get("project") or window_id],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "add":
        add_pane(stdscr, tower, window_id)
    elif pick.selected_key == "rename":
        rename_window(stdscr, tower, window_id)
    elif pick.selected_key == "layout":
        choose_layout(stdscr, tower, window_id)
    elif pick.selected_key == "close":
        close_window(stdscr, tower, window_id)


def _manage_panes(stdscr, tower, window_id: str) -> str:
    tower.load()
    panes = [row for row in tower.rows if row.get("window_id") == window_id and row.get("pane_id") and not row.get("remote")]
    if not panes:
        show_message_screen(stdscr, t("struct.manage_panes"), [t("zero.title")])
        return ""
    items = [(row["key"], f'{row.get("pane_id")}  {row.get("project") or "-"}  {row.get("agent") or "-"}') for row in panes]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(stdscr, t("struct.manage_panes"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return ""
    from .control_view import open_control_view

    open_control_view(stdscr, tower, pick.selected_key)
    return ""


def _confirm(stdscr, title: str, lines: List[str]) -> bool:
    pick = run_list_picker(
        stdscr,
        title,
        [("cancel", t("control.cancel")), ("yes", t("struct.confirm_yes"))],
        footer_hint=t("wizard.hint_list"),
        preamble=lines,
    )
    return not pick.cancelled and pick.selected_key == "yes"


def _preferred_window(tower) -> str:
    if tower.visible_rows and 0 <= tower.selected < len(tower.visible_rows):
        row = tower.visible_rows[tower.selected]
        if row.get("window_id"):
            return row["window_id"]
    return structure.current_window_id(tower.session)


def _pick_window(stdscr, tower) -> Optional[str]:
    windows = structure.list_windows(tower.session)
    items = [(item["window_id"], f'{item["window_id"]}  {item["window_index"]}: {item["window_name"]}') for item in windows]
    items.append(("cancel", t("menu.cancel")))
    pick = run_list_picker(stdscr, t("struct.place_pick"), items, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return None
    return pick.selected_key


def _window_meta(tower, window_id: str) -> dict:
    for item in structure.list_windows(tower.session):
        if item["window_id"] == window_id:
            return item
    for row in tower.rows:
        if row.get("window_id") == window_id:
            return {
                "window_id": window_id,
                "window_name": row.get("window_name") or "",
                "pane_count": 0,
            }
    return {"window_id": window_id, "window_name": "", "pane_count": 0}


def _state_dir():
    from .tower import STATE_DIR

    return STATE_DIR

