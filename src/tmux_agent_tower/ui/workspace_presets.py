"""Create, edit, run, and remove saved workspace launch choices."""

from __future__ import annotations

from pathlib import Path

from ..i18n import t
from ..launcher.config import load_config
from ..launcher.plan import LaunchPlanError, WorkspaceRef, execute_launch_plan
from ..launcher.presets import (
    PresetStoreError,
    WorkspacePreset,
    WorkspacePresetStore,
    new_preset,
)
from .widgets import prompt_text, run_list_picker, show_message_screen


def open_workspace_presets(stdscr, tower, state_dir: Path) -> None:
    store = WorkspacePresetStore()
    while True:
        try:
            presets = store.list()
        except PresetStoreError as exc:
            show_message_screen(stdscr, t("preset.title"), [str(exc)])
            return
        items = [("new", t("preset.new"))]
        items.extend((preset.preset_id, _preset_label(preset)) for preset in presets)
        items.append(("back", t("menu.cancel")))
        pick = run_list_picker(
            stdscr,
            t("preset.title"),
            items,
            searchable=True,
            footer_hint=t("preset.list_hint"),
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "new":
            _edit_preset(stdscr, tower, state_dir, store)
            continue
        preset = next((row for row in presets if row.preset_id == pick.selected_key), None)
        if preset is None:
            continue
        action = run_list_picker(
            stdscr,
            t("preset.title"),
            [
                ("run", t("preset.run")),
                ("edit", t("preset.edit")),
                ("delete", t("preset.delete")),
                ("back", t("menu.cancel")),
            ],
            footer_hint=t("wizard.hint_list"),
            preamble=[_preset_label(preset)],
        )
        if action.cancelled or action.selected_key in (None, "back"):
            continue
        if action.selected_key == "run":
            _run_preset(stdscr, tower, state_dir, preset)
        elif action.selected_key == "edit":
            _edit_preset(stdscr, tower, state_dir, store, preset)
        elif action.selected_key == "delete":
            _delete_preset(stdscr, store, preset)


def _edit_preset(stdscr, tower, state_dir: Path, store: WorkspacePresetStore, old=None) -> None:
    from .workspace_browser import CREATE_AGENT_ORDER, _path_preamble, _pick_layout, pick_workspaces

    name = prompt_text(stdscr, t("preset.name"), initial=old.name if old else "")
    if name is None or not name.strip():
        return
    picks = pick_workspaces(stdscr, tower, state_dir, multi=True)
    if not picks:
        return
    preamble = _path_preamble(picks)
    checked = set(old.agents) if old else set()
    selected = run_list_picker(
        stdscr,
        t("preset.pick_agents"),
        [(agent, agent) for agent in CREATE_AGENT_ORDER],
        multi=True,
        checked=checked,
        footer_hint=t("preset.agent_hint"),
        preamble=preamble,
    )
    if selected.cancelled or not selected.selected_keys:
        return
    agents = tuple(agent for agent in CREATE_AGENT_ORDER if agent in selected.selected_keys)
    laid = _pick_layout(stdscr, preamble + [", ".join(agents)], "new")
    if laid is None:
        return
    layout_choice, layout_label = laid
    layout = {
        "auto": "tiled",
        "horizontal": "even-horizontal",
        "vertical": "even-vertical",
        "main-horizontal": "main-horizontal",
        "main-vertical": "main-vertical",
    }.get(layout_choice, layout_choice)
    refs = tuple(WorkspaceRef(pick.entry.name, pick.entry.path) for pick in picks)
    try:
        preset = new_preset(
            preset_id=old.preset_id if old else "",
            name=name,
            host_key=picks[0].host_key,
            host_label=picks[0].host_label,
            is_remote=picks[0].is_remote,
            workspaces=refs,
            agents=agents,
            layout=layout,
        )
    except PresetStoreError as exc:
        show_message_screen(stdscr, t("preset.title"), [str(exc)])
        return
    confirm = run_list_picker(
        stdscr,
        t("preset.save_title"),
        [("save", t("preset.save")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=[_preset_label(preset), t("preset.layout_line", layout=layout_label)],
    )
    if confirm.cancelled or confirm.selected_key != "save":
        return
    try:
        store.save(preset)
    except PresetStoreError as exc:
        show_message_screen(stdscr, t("preset.title"), [str(exc)])
        return
    show_message_screen(stdscr, t("preset.title"), [t("preset.saved", name=preset.name)])


def _run_preset(stdscr, tower, state_dir: Path, preset: WorkspacePreset) -> None:
    host = _current_host(tower, preset)
    if host is None:
        show_message_screen(stdscr, t("preset.run_failed"), [t("preset.host_missing", host=preset.host_label)])
        return
    plan = preset.launch_plan()
    confirm = run_list_picker(
        stdscr,
        t("preset.run_title"),
        [("run", t("preset.run_confirm")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=[
            _preset_label(preset),
            t("browser.host", host=host[1]),
            t("preset.agent_line", agents=", ".join(plan.agents)),
            t("preset.layout_line", layout=t(f"struct.layout.{plan.layout}")),
        ] + [workspace.path for workspace in plan.workspaces],
    )
    if confirm.cancelled or confirm.selected_key != "run":
        return
    try:
        results = execute_launch_plan(
            plan,
            session=tower.session,
            state_dir=state_dir,
            agents_cfg=load_config()["agents"],
            overrides=tower.overrides,
        )
    except LaunchPlanError as exc:
        show_message_screen(stdscr, t("preset.run_failed"), [str(exc)])
        return
    lines = []
    for result in results:
        if result.ok:
            lines.append(t("preset.started", name=result.target.project_name, agent=result.target.agent_label))
        else:
            lines.append(t("preset.not_started", name=result.target.project_name, detail=result.detail))
    show_message_screen(stdscr, t("preset.result"), lines or [t("preset.no_results")])


def _delete_preset(stdscr, store: WorkspacePresetStore, preset: WorkspacePreset) -> None:
    confirm = run_list_picker(
        stdscr,
        t("preset.delete_title"),
        [("delete", t("preset.delete_confirm")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=[_preset_label(preset)],
    )
    if confirm.cancelled or confirm.selected_key != "delete":
        return
    try:
        store.delete(preset.preset_id)
    except PresetStoreError as exc:
        show_message_screen(stdscr, t("preset.title"), [str(exc)])
        return
    show_message_screen(stdscr, t("preset.title"), [t("preset.deleted", name=preset.name)])


def _current_host(tower, preset: WorkspacePreset):
    if preset.is_remote:
        row = next((host for host in tower.remote_hosts if host.get("alias") == preset.host_key), None)
        if row is None:
            return None
        return preset.host_key, row.get("name") or preset.host_key
    if tower.local_host != preset.host_key:
        return None
    return preset.host_key, tower.local_host


def _preset_label(preset: WorkspacePreset) -> str:
    projects = ", ".join(workspace.name for workspace in preset.workspaces)
    agents = ", ".join(preset.agents)
    layout = t(f"struct.layout.{preset.layout}")
    return f"{preset.name}  ·  {preset.host_label}  ·  {projects}  ·  {agents}  ·  {layout}"
