"""TUI flows for saving and launching real Work Groups."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
import uuid

from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER, load_config, resolve_agent_command
from ..launcher.workset_launch import WorksetLaunchError, launch_workset
from ..state.overrides import ROLE_IDS
from ..state.worksets import WorkMember, WorksetError, WorksetStore, layout_for_count, member_slots, new_workset
from .widgets import prompt_text, run_list_picker, show_message_screen


def save_work_group(stdscr, tower, group: dict) -> None:
    kind_pick = run_list_picker(
        stdscr, t("workset.save_title"),
        [("saved_work", t("workset.save_as_work")), ("template", t("workset.save_as_template")),
         ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
    )
    kind = kind_pick.selected_key
    if kind not in {"saved_work", "template"}:
        return
    name = prompt_text(stdscr, t("workset.name_prompt"), initial=group.get("display_name") or "")
    if not name:
        return
    tower.load()
    by_target = {str(row.get("target_id")): row for row in tower.rows if row.get("target_id")}
    live = [by_target.get(target) for target in group.get("member_order", [])]
    if not live or any(row is None for row in live):
        show_message_screen(stdscr, t("workset.save_failed"), [t("workset.stale_member")])
        return
    members = []
    slots = group.get("layout_slots") or {}
    for target_id, row in zip(group.get("member_order", []), live):
        role = row.get("role") or ""
        agent = row.get("agent") or ""
        path = str(row.get("project_path") or row.get("path") or "")
        project = str(row.get("project") or "")
        if not agent or (kind == "saved_work" and not path):
            show_message_screen(stdscr, t("workset.save_failed"), [t("workset.member_incomplete")])
            return
        if kind == "template":
            members.append(WorkMember(
                "", role, agent, execution_target="auto", layout_slot=slots.get(target_id, ""),
            ))
        else:
            execution = "auto"
            if row.get("remote"):
                execution = str(row.get("result_provider_host") or row.get("host") or "auto")
            elif row.get("key") and row.get("session") and row.get("pane_pid"):
                execution = tower.overrides.get_execution_host(
                    row["key"], str(row["session"]), str(row["pane_pid"])
                ) or "auto"
            members.append(WorkMember(
                "", role, agent,
                display_name=str(row.get("display_name") or row.get("task_name") or ""),
                name_origin=row.get("name_origin") if row.get("name_origin") in {"auto", "user"} else "auto",
                project_name=project,
                project_path=path,
                execution_target=execution,
                layout_slot=slots.get(target_id, ""),
            ))
    try:
        workset = new_workset(
            kind=kind, name=name, members=members,
            layout=group.get("layout") or layout_for_count(len(members)),
        )
        WorksetStore().save(workset)
    except (OSError, WorksetError, ValueError) as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])
        return
    show_message_screen(stdscr, t("workset.saved"), [name])


def open_saved_work(stdscr, tower, state_dir: Path) -> None:
    _open_collection(stdscr, tower, state_dir, "saved_work")


def open_work_templates(stdscr, tower, state_dir: Path) -> None:
    _open_collection(stdscr, tower, state_dir, "template")


def open_saved_collection(stdscr, tower, state_dir: Path) -> None:
    """One user entry for both reusable work and reusable templates."""
    pick = run_list_picker(
        stdscr,
        t("workset.collection_title"),
        [
            ("saved_work", t("workset.saved_section")),
            ("template", t("workset.template_section")),
            ("back", t("menu.cancel")),
        ],
        footer_hint=t("wizard.hint_list"),
        preamble=[t("workset.saved_help"), t("workset.template_help")],
    )
    if pick.selected_key == "saved_work":
        open_saved_work(stdscr, tower, state_dir)
    elif pick.selected_key == "template":
        open_work_templates(stdscr, tower, state_dir)


def _open_collection(stdscr, tower, state_dir: Path, kind: str) -> None:
    title = t("workset.saved_title" if kind == "saved_work" else "workset.template_title")
    while True:
        store = WorksetStore()
        try:
            worksets = store.list(kind)
        except WorksetError as exc:
            show_message_screen(stdscr, title, [t("workset.load_failed")])
            return
        if not worksets:
            empty = "workset.empty_saved" if kind == "saved_work" else "workset.empty_template"
            show_message_screen(stdscr, title, [t(empty)])
            return
        items = [(item.workset_id, _label(item)) for item in worksets]
        items.append(("back", t("menu.cancel")))
        selected = run_list_picker(
            stdscr, title, items, searchable=True,
            footer_hint=t("workset.list_hint"),
        )
        if selected.cancelled or selected.selected_key in (None, "back"):
            return
        item = next((row for row in worksets if row.workset_id == selected.selected_key), None)
        if item is None:
            continue
        if kind == "template":
            actions = [("open", t("workset.load_template")), ("rename", t("workset.rename")),
                       ("members", t("workset.edit_members")), ("agent", t("workset.edit_agents")),
                       ("layout", t("workset.edit_layout")),
                       ("duplicate", t("workset.duplicate")), ("delete", t("workset.delete")),
                       ("back", t("menu.cancel"))]
        else:
            actions = [("open", t("workset.open")), ("configure", t("workset.configure_open")),
                       ("rename", t("workset.rename")), ("members", t("workset.edit_members")),
                       ("locations", t("workset.edit_locations")),
                       ("path", t("workset.edit_project")),
                       ("agent", t("workset.edit_agents")), ("clone", t("workset.clone_template")),
                       ("delete", t("workset.delete")), ("back", t("menu.cancel"))]
        action = run_list_picker(
            stdscr, title, actions, footer_hint=t("wizard.hint_list"),
            preamble=_preview_lines(item, tower=tower),
        )
        if action.cancelled or action.selected_key in (None, "back"):
            continue
        key = action.selected_key
        if key == "open":
            _open_item(stdscr, tower, state_dir, item, configure=(kind == "template"))
        elif key == "configure":
            _open_item(stdscr, tower, state_dir, item, configure=True)
        elif key == "rename":
            _rename(stdscr, store, item)
        elif key == "members":
            _edit_members(stdscr, tower, state_dir, store, item)
        elif key == "locations":
            _edit_locations(stdscr, tower, store, item)
        elif key == "path":
            _change_saved_path(stdscr, tower, state_dir, store, item)
        elif key == "agent":
            _edit_agents(stdscr, store, item)
        elif key == "layout":
            _edit_layout(stdscr, store, item)
        elif key in {"clone", "duplicate"}:
            _clone_template(stdscr, store, item)
        elif key == "delete":
            _delete(stdscr, store, item)


def _open_item(stdscr, tower, state_dir: Path, workset, *, configure: bool) -> None:
    project_override = None
    execution_target = None
    execution_targets = {}
    agent_overrides = {}
    if configure:
        from .workspace_browser import pick_workspaces

        picked = pick_workspaces(stdscr, tower, state_dir, multi=False)
        if not picked:
            return
        place = picked[0]
        project_override = (place.entry.name, place.entry.path)
        execution_target = place.host_key
        selected = _pick_agent_overrides(stdscr, workset, place.is_remote)
        if selected is None:
            return
        agent_overrides = selected
        execution_target = place.host_key
    actual_name = workset.name
    if workset.kind == "template" and project_override:
        actual_name = f"{project_override[0]} · {workset.name}"
    while True:
        preview = _preview_lines(workset, project_override, agent_overrides,
                                 execution_target, execution_targets, tower)
        confirm = run_list_picker(
            stdscr, t("workset.preview_title"),
            [("start", t("workset.start")), ("locations", t("workset.edit_execution_locations")),
             ("cancel", t("menu.cancel"))],
            footer_hint=t("wizard.hint_list"), preamble=preview,
        )
        if confirm.cancelled or confirm.selected_key in (None, "cancel"):
            return
        if confirm.selected_key == "locations":
            selected_targets = _pick_execution_targets(stdscr, tower, workset, execution_target)
            if selected_targets is None:
                return
            execution_targets = selected_targets
            execution_target = None
            continue
        if confirm.selected_key == "start":
            break
    try:
        result = launch_workset(
            tower, workset, state_dir, agents_cfg=load_config()["agents"],
            project_override=project_override, agent_overrides=agent_overrides,
            execution_target=execution_target, execution_targets=execution_targets,
            group_name=actual_name,
        )
    except WorksetLaunchError as exc:
        show_message_screen(stdscr, t("workset.start_failed"), [str(exc)])
        return
    show_message_screen(stdscr, t("workset.started"), [result.group_name, t("workset.member_count").format(n=len(result.members))])


def _pick_execution_targets(stdscr, tower, workset, default_target: str | None) -> dict | None:
    choices = [("auto", t("workset.execution_auto")),
               (tower.local_host, t("workset.execution_local", host=tower.local_host))]
    choices.extend((str(row["alias"]), str(row.get("name") or row["alias"]))
                   for row in tower.remote_hosts if row.get("alias"))
    selected = {}
    for member in workset.members:
        role = t(f"role.{member.role}") if member.role in ROLE_IDS else t("role.unassigned")
        current = default_target or member.execution_target or "auto"
        pick = run_list_picker(
            stdscr, t("workset.location_for_role", role=role), choices,
            footer_hint=t("wizard.hint_list"),
            preamble=[t("workset.execution_current", host=_execution_label(tower, current))],
        )
        if pick.cancelled or pick.selected_key not in dict(choices):
            return None
        selected[member.member_id] = pick.selected_key
    return selected


def _pick_agent_overrides(stdscr, workset, remote: bool) -> dict | None:
    config = load_config()["agents"]
    chosen = {}
    for member in workset.members:
        role = t(f"role.{member.role}") if member.role in ROLE_IDS else t("role.unassigned")
        options = [("keep", t("workset.keep_agent", agent=member.agent))]
        for agent in AGENT_LAUNCH_ORDER:
            available = remote or resolve_agent_command(agent, config) is not None
            label = agent if available else t("workset.agent_unavailable", agent=agent)
            options.append((agent, label))
        pick = run_list_picker(
            stdscr, t("workset.agent_for_role", role=role), options,
            footer_hint=t("wizard.hint_list"), preamble=[t("workset.agent_default", agent=member.agent)],
        )
        if pick.cancelled:
            return None
        if pick.selected_key not in (None, "keep"):
            chosen[member.member_id] = pick.selected_key
    return chosen


def _rename(stdscr, store: WorksetStore, workset) -> None:
    name = prompt_text(stdscr, t("workset.name_prompt"), initial=workset.name)
    if not name:
        return
    try:
        store.save(replace(workset, name=name.strip(), updated_at=time.time()))
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _edit_agents(stdscr, store: WorksetStore, workset) -> None:
    config = load_config()["agents"]
    members = list(workset.members)
    for index, member in enumerate(members):
        role = t(f"role.{member.role}") if member.role in ROLE_IDS else t("role.unassigned")
        options = [(agent, agent if resolve_agent_command(agent, config) else t("workset.agent_unavailable", agent=agent))
                   for agent in AGENT_LAUNCH_ORDER]
        pick = run_list_picker(
            stdscr, t("workset.agent_for_role", role=role), options,
            footer_hint=t("wizard.hint_list"), preamble=[t("workset.agent_default", agent=member.agent)],
        )
        if pick.cancelled or not pick.selected_key:
            return
        members[index] = replace(member, agent=pick.selected_key)
    try:
        store.save(replace(workset, members=tuple(members), updated_at=time.time()))
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _edit_locations(stdscr, tower, store: WorksetStore, workset) -> None:
    if workset.kind != "saved_work":
        return
    members = list(workset.members)
    hosts = [("auto", t("workset.execution_auto")),
             (tower.local_host, t("workset.execution_local", host=tower.local_host))]
    hosts.extend((str(row["alias"]), str(row.get("name") or row["alias"]))
                 for row in tower.remote_hosts if row.get("alias"))
    for index, member in enumerate(members):
        role = t(f"role.{member.role}") if member.role in ROLE_IDS else t("role.unassigned")
        selected = run_list_picker(
            stdscr, t("workset.location_for_role", role=role), hosts,
            footer_hint=t("wizard.hint_list"),
            preamble=[t("workset.execution_current", host=_execution_label(tower, member.execution_target))],
        )
        if selected.cancelled or not selected.selected_key:
            return
        members[index] = replace(member, execution_target=selected.selected_key)
    try:
        store.save(replace(workset, members=tuple(members), updated_at=time.time()))
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _edit_layout(stdscr, store: WorksetStore, workset) -> None:
    choices = [(key, t(f"workset.layout.{key}")) for key in ("focus", "split-2", "grid-4", "main-plus-side")]
    pick = run_list_picker(stdscr, t("workset.edit_layout"), choices, footer_hint=t("wizard.hint_list"))
    if pick.cancelled or pick.selected_key not in dict(choices):
        return
    try:
        store.save(replace(workset, layout=pick.selected_key, updated_at=time.time()))
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _edit_members(stdscr, tower, state_dir: Path, store: WorksetStore, workset) -> None:
    members = list(workset.members)
    order = list(workset.member_order)
    while True:
        by_id = {member.member_id: member for member in members}
        labels = {
            member_id: " · ".join(filter(None, (
                t(f"role.{by_id[member_id].role}") if by_id[member_id].role in ROLE_IDS else t("role.unassigned"),
                by_id[member_id].display_name,
                by_id[member_id].agent,
            )))
            for member_id in order
        }
        items = [(member_id, labels[member_id]) for member_id in order]
        items.extend([("add", t("workset.add_member")), ("done", t("menu.cancel"))])
        pick = run_list_picker(
            stdscr, t("workset.edit_members"), items,
            footer_hint=t("wizard.hint_list"), preamble=[workset.name],
        )
        if pick.cancelled or pick.selected_key in (None, "done"):
            return
        if pick.selected_key == "add":
            roles = [(role, t(f"role.{role}")) for role in ROLE_IDS]
            roles.append(("", t("role.unassigned")))
            role = run_list_picker(stdscr, t("workset.member_role"), roles, footer_hint=t("wizard.hint_list"))
            if role.cancelled or role.selected_key is None:
                continue
            config = load_config()["agents"]
            agent = run_list_picker(
                stdscr, t("workset.member_agent"),
                [(name, name if resolve_agent_command(name, config) else t("workset.agent_unavailable", agent=name))
                 for name in AGENT_LAUNCH_ORDER],
                footer_hint=t("wizard.hint_list"),
            )
            if agent.cancelled or not agent.selected_key:
                continue
            project_name = project_path = ""
            execution_target = "auto"
            if workset.kind == "saved_work":
                first = members[0]
                project_name, project_path, execution_target = first.project_name, first.project_path, first.execution_target
                if len({(row.project_name, row.project_path, row.execution_target) for row in members}) > 1:
                    from .workspace_browser import pick_workspaces

                    picked = pick_workspaces(stdscr, tower, state_dir, multi=False)
                    if not picked:
                        continue
                    place = picked[0]
                    project_name, project_path, execution_target = place.entry.name, place.entry.path, place.host_key
            member = WorkMember(
                uuid.uuid4().hex, role.selected_key, agent.selected_key,
                project_name=project_name, project_path=project_path,
                execution_target=execution_target,
                layout_slot=member_slots(len(members) + 1)[-1],
            )
            members.append(member)
            order.append(member.member_id)
        else:
            member = by_id.get(pick.selected_key)
            if member is None:
                continue
            action = run_list_picker(
                stdscr, t("workset.edit_members"),
                ([("name", t("workset.rename_member"))] if workset.kind == "saved_work" else []
                 + [("role", t("workset.change_role")), ("up", t("group.move_up")),
                    ("down", t("group.move_down")), ("remove", t("workset.remove_member")),
                    ("back", t("menu.cancel"))]),
                footer_hint=t("wizard.hint_list"), preamble=[labels[member.member_id]],
            )
            key = action.selected_key
            if key == "name" and workset.kind == "saved_work":
                name = prompt_text(stdscr, t("workset.member_name_prompt"), initial=member.display_name)
                if name is not None:
                    name = name.strip()
                    members[members.index(member)] = replace(
                        member, display_name=name, name_origin="user" if name else "auto",
                    )
            elif key == "role":
                roles = [(role, t(f"role.{role}")) for role in ROLE_IDS]
                roles.append(("", t("role.unassigned")))
                selected = run_list_picker(stdscr, t("workset.member_role"), roles, footer_hint=t("wizard.hint_list"))
                if not selected.cancelled and selected.selected_key is not None:
                    members[members.index(member)] = replace(member, role=selected.selected_key)
            elif key in {"up", "down"}:
                index = order.index(member.member_id)
                delta = -1 if key == "up" else 1
                new_index = max(0, min(len(order) - 1, index + delta))
                order.insert(new_index, order.pop(index))
            elif key == "remove" and len(members) > 1:
                members.remove(member)
                order.remove(member.member_id)
        try:
            store.save(replace(workset, members=tuple(members), member_order=tuple(order), updated_at=time.time()))
            workset = next(row for row in store.list(workset.kind) if row.workset_id == workset.workset_id)
            members, order = list(workset.members), list(workset.member_order)
        except WorksetError as exc:
            show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])
            return


def _change_saved_path(stdscr, tower, state_dir: Path, store: WorksetStore, workset) -> None:
    from .workspace_browser import pick_workspaces

    picked = pick_workspaces(stdscr, tower, state_dir, multi=False)
    if not picked:
        return
    place = picked[0]
    members = tuple(replace(
        row, project_name=place.entry.name, project_path=place.entry.path,
        execution_target=place.host_key,
    ) for row in workset.members)
    try:
        store.save(replace(workset, members=members, updated_at=time.time()))
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _clone_template(stdscr, store: WorksetStore, workset) -> None:
    name = prompt_text(stdscr, t("workset.template_name_prompt"), initial=workset.name)
    if not name:
        return
    members = [WorkMember("", row.role, row.agent, layout_slot=row.layout_slot) for row in workset.members]
    try:
        store.save(new_workset(kind="template", name=name, members=members, layout=workset.layout))
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _delete(stdscr, store: WorksetStore, workset) -> None:
    confirm = run_list_picker(
        stdscr, t("workset.delete_title"),
        [("delete", t("workset.delete_confirm")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"), preamble=[workset.name],
    )
    if confirm.cancelled or confirm.selected_key != "delete":
        return
    try:
        store.delete(workset.workset_id)
    except WorksetError as exc:
        show_message_screen(stdscr, t("workset.save_failed"), [str(exc)])


def _preview_lines(workset, project_override=None, agent_overrides=None,
                   execution_target=None, execution_targets=None, tower=None) -> list[str]:
    agent_overrides = agent_overrides or {}
    execution_targets = execution_targets or {}
    lines = [workset.name]
    if project_override:
        lines.extend([t("detail.project") + ": " + project_override[0], project_override[1]])
    else:
        paths = list(dict.fromkeys(member.project_path for member in workset.members if member.project_path))
        names = list(dict.fromkeys(member.project_name for member in workset.members if member.project_name))
        if names:
            lines.append(t("detail.project") + ": " + ", ".join(names))
        if paths:
            lines.extend([t("detail.path") + ":", *paths])
    for member in workset.members:
        role = t(f"role.{member.role}") if member.role in ROLE_IDS else t("role.unassigned")
        agent = agent_overrides.get(member.member_id, member.agent)
        location = execution_targets.get(member.member_id, execution_target or member.execution_target or "auto")
        if tower:
            location = _execution_label(tower, location)
        elif location == "auto":
            location = t("workset.execution_auto")
        lines.append(f"{role or member.display_name or t('task.unnamed')}  ·  {agent}  ·  {location}")
    lines.append(t("workset.layout_line", layout=t(f"workset.layout.{workset.layout}")))
    return lines


def _label(workset) -> str:
    count = len(workset.members)
    project = next((member.project_name for member in workset.members if member.project_name), "")
    prefix = f"{project} · " if project else ""
    return f"{prefix}{workset.name}  ·  {count}개"


def _execution_label(tower, target: str) -> str:
    if not target or target == "auto":
        return t("workset.execution_auto")
    if target == tower.local_host:
        return t("workset.execution_local", host=tower.local_host)
    remote = next((row for row in tower.remote_hosts if target in (row.get("alias"), row.get("name"))), None)
    return str(remote.get("name") or remote.get("alias")) if remote else target
