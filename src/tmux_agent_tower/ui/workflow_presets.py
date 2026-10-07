"""Human-controlled workflow run creation, review, and stage actions."""

from __future__ import annotations

import curses
from pathlib import Path

from ..control import actions
from ..i18n import t
from ..launcher.presets import PresetStoreError, WorkspacePresetStore
from ..launcher.workflows import (
    HumanGateStage,
    RoleStage,
    WORKFLOW_PRESETS,
    WorkflowPreset,
    WorkflowPresetFileError,
    load_custom_workflows,
)
from ..role_harness import HarnessError, load_harness
from ..state.workflow_runs import (
    PaneIdentity,
    RUN_FILE_NAME,
    WorkflowRun,
    WorkflowRunError,
    WorkflowRunStore,
    WorkflowStageRun,
)
from ..control.actions import MAX_PROMPT_CHARS
from .prompt_composer import prompt_composer
from .widgets import is_enter, is_escape, read_key, run_list_picker, safe_add, show_message_screen


def open_workflow_presets(stdscr, tower, state_dir: Path) -> None:
    """Open workflow templates and persisted runs; selecting never dispatches."""

    store = WorkflowRunStore(Path(state_dir) / RUN_FILE_NAME)
    custom_error_shown = False
    while True:
        try:
            workflows = (*WORKFLOW_PRESETS, *load_custom_workflows())
        except WorkflowPresetFileError:
            workflows = WORKFLOW_PRESETS
            if not custom_error_shown:
                show_message_screen(stdscr, t("workflow.title"), [t("workflow.custom_invalid")])
                custom_error_shown = True
        try:
            runs = store.list()
        except WorkflowRunError as exc:
            show_message_screen(stdscr, t("workflow.title"), [str(exc)])
            return
        items = [("new", t("workflow.new_run")), ("runs", t("workflow.runs"))]
        items.extend((f"template:{row.preset_id}", f'{t("workflow.template_prefix")} · {row.name}') for row in workflows)
        items.append(("back", t("menu.cancel")))
        pick = run_list_picker(stdscr, t("workflow.title"), items, footer_hint=t("wizard.hint_list"))
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "new":
            _create_run(stdscr, tower, store, workflows)
        elif pick.selected_key == "runs":
            _open_runs(stdscr, tower, store)
        elif str(pick.selected_key).startswith("template:"):
            preset_id = str(pick.selected_key).split(":", 1)[1]
            preset = next((row for row in workflows if row.preset_id == preset_id), None)
            if preset is not None:
                _preview_template(stdscr, preset)


def _create_run(stdscr, tower, store: WorkflowRunStore, workflows=None) -> None:
    workflows = tuple(workflows if workflows is not None else WORKFLOW_PRESETS)
    workspace_store = WorkspacePresetStore()
    try:
        workspaces = workspace_store.list()
    except PresetStoreError as exc:
        show_message_screen(stdscr, t("workflow.title"), [str(exc)])
        return
    if not workspaces:
        show_message_screen(stdscr, t("workflow.title"), [t("workflow.no_workspaces")])
        return
    selected = run_list_picker(
        stdscr,
        t("workflow.pick_workspace"),
        [(row.preset_id, _workspace_label(row)) for row in workspaces] + [("cancel", t("menu.cancel"))],
        searchable=True,
        footer_hint=t("wizard.hint_list"),
    )
    if selected.cancelled or selected.selected_key in (None, "cancel"):
        return
    workspace = next((row for row in workspaces if row.preset_id == selected.selected_key), None)
    if workspace is None:
        return
    selected = run_list_picker(
        stdscr,
        t("workflow.pick_template"),
        [(row.preset_id, row.name) for row in workflows] + [("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
    )
    if selected.cancelled or selected.selected_key in (None, "cancel"):
        return
    workflow = next((row for row in workflows if row.preset_id == selected.selected_key), None)
    if workflow is None:
        return
    confirm = run_list_picker(
        stdscr,
        t("workflow.create_title"),
        [("create", t("workflow.create_confirm")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=[
            _workspace_label(workspace),
            workflow.name,
            *_stage_labels(workflow),
            t("workflow.preview_only"),
        ],
    )
    if confirm.cancelled or confirm.selected_key != "create":
        return
    try:
        run = store.create(workspace, workflow)
    except WorkflowRunError as exc:
        show_message_screen(stdscr, t("workflow.title"), [str(exc)])
        return
    show_message_screen(stdscr, t("workflow.created"), [t("workflow.run_id", run_id=run.run_id[:12])])
    _open_run_detail(stdscr, tower, store, run.run_id)


def _open_runs(stdscr, tower, store: WorkflowRunStore) -> None:
    while True:
        try:
            runs = store.list()
        except WorkflowRunError as exc:
            show_message_screen(stdscr, t("workflow.title"), [str(exc)])
            return
        if not runs:
            show_message_screen(stdscr, t("workflow.title"), [t("workflow.no_runs")])
            return
        items = [(run.run_id, _run_label(run)) for run in reversed(runs)]
        items.append(("back", t("menu.cancel")))
        pick = run_list_picker(stdscr, t("workflow.runs"), items, searchable=True, footer_hint=t("wizard.hint_list"))
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if any(run.run_id == pick.selected_key for run in runs):
            _open_run_detail(stdscr, tower, store, str(pick.selected_key))


def _open_run_detail(stdscr, tower, store: WorkflowRunStore, run_id: str) -> None:
    while True:
        try:
            run = store.get(run_id)
        except WorkflowRunError as exc:
            show_message_screen(stdscr, t("workflow.title"), [str(exc)])
            return
        index = _current_stage_index(run)
        if index is None:
            show_message_screen(stdscr, t("workflow.done"), _run_lines(run))
            return
        stage = run.stages[index]
        lines = _run_lines(run, current_index=index)
        if stage.kind == "role" and stage.state != "PENDING":
            lines.extend(_running_target_lines(tower, run, stage))

        if stage.state == "RUNNING":
            actions_to_show = [
                ("PASS", t("workflow.record_pass")),
                ("FAIL", t("workflow.record_fail")),
                ("BLOCKED", t("workflow.record_blocked")),
            ]
        elif stage.state in ("FAIL", "BLOCKED"):
            actions_to_show = [("retry", t("workflow.retry"))]
        elif stage.kind == "human_gate":
            actions_to_show = [("start", t("workflow.begin_human"))]
        else:
            actions_to_show = [("start", t("workflow.next_start"))]
        actions_to_show.append(("back", t("menu.cancel")))
        pick = run_list_picker(
            stdscr,
            t("workflow.run_title", name=run.workflow_name),
            actions_to_show,
            footer_hint=t("wizard.hint_list"),
            preamble=lines,
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        try:
            if pick.selected_key in ("PASS", "FAIL", "BLOCKED"):
                store.finish_stage(run_id, index, str(pick.selected_key))
            elif pick.selected_key == "retry":
                store.retry_stage(run_id, index)
            elif pick.selected_key == "start" and stage.kind == "human_gate":
                store.start_stage(run_id, index)
            elif pick.selected_key == "start" and stage.kind == "role":
                _start_role_stage(stdscr, tower, store, run, index, stage)
        except WorkflowRunError as exc:
            show_message_screen(stdscr, t("workflow.title"), [str(exc)])


def _start_role_stage(
    stdscr, tower, store: WorkflowRunStore, run: WorkflowRun, index: int, stage: WorkflowStageRun
) -> None:
    try:
        harness = load_harness(stage.role_id or "")
    except HarnessError as exc:
        show_message_screen(stdscr, t("workflow.title"), [str(exc)])
        return
    if not _show_harness_preview(stdscr, stage.role_id or "", harness):
        return
    if run.is_remote or run.host_key != getattr(tower, "local_host", ""):
        show_message_screen(stdscr, t("workflow.no_panes"), [t("workflow.remote_dispatch_unavailable")])
        return
    tower.load()
    candidates = _compatible_panes(tower, run)
    if not candidates:
        show_message_screen(stdscr, t("workflow.no_panes"), [t("workflow.no_compatible_panes")])
        return
    selected = run_list_picker(
        stdscr,
        t("workflow.pick_pane"),
        [(row["key"], _pane_label(row)) for row in candidates] + [("cancel", t("menu.cancel"))],
        searchable=True,
        footer_hint=t("wizard.hint_list"),
        preamble=[_workspace_label_from_run(run)],
    )
    if selected.cancelled or selected.selected_key in (None, "cancel"):
        return
    chosen = next((row for row in candidates if row["key"] == selected.selected_key), None)
    if chosen is None:
        return
    brief = prompt_composer(
        stdscr,
        t("workflow.brief_title"),
        context_lines=[
            t(f"role.{stage.role_id}"),
            t("workflow.brief_context", task=chosen.get("task_name") or chosen.get("project") or "-"),
        ],
    )
    if brief is None or not brief.strip():
        return
    message = _compose_prompt(stage.role_id or "", harness, brief.strip())
    if len(message) > MAX_PROMPT_CHARS:
        show_message_screen(
            stdscr,
            t("workflow.title"),
            [t("control.composer_too_long", limit=f"{MAX_PROMPT_CHARS:,}")],
        )
        return
    identity = _pane_identity_fields(chosen)
    provider = str(chosen.get("auto_agent") or "")
    task_label = str(chosen.get("task_name") or chosen.get("project") or t("project.no_name"))
    if not _show_prompt_preview(stdscr, stage.role_id or "", provider, task_label, chosen, message):
        return
    confirm = run_list_picker(
        stdscr,
        t("workflow.send_title"),
        [("send", t("workflow.send_confirm")), ("cancel", t("menu.cancel"))],
        footer_hint=t("wizard.hint_list"),
        preamble=[
            t("workflow.role_line", role=t(f"role.{stage.role_id}")),
            t("workflow.provider_line", provider=provider),
            t("workflow.task_line", task=task_label),
            f'{t("detail.pane_id")}  {chosen.get("pane_id") or chosen.get("key")}',
            t("workflow.brief_will_send"),
        ],
    )
    if confirm.cancelled or confirm.selected_key != "send":
        return

    tower.load()
    current = next((row for row in tower.rows if row.get("key") == chosen.get("key")), None)
    if current is None or not _same_pane(current, chosen, run):
        show_message_screen(stdscr, t("workflow.stale_pane"), [t("workflow.reselect_pane")])
        return
    pane_identity = PaneIdentity(str(chosen["key"]), str(chosen["session"]), str(chosen["pane_pid"]))
    try:
        store.start_stage(run.run_id, index, pane=pane_identity, provider=provider, task_label=task_label)
    except WorkflowRunError as exc:
        show_message_screen(stdscr, t("workflow.title"), [str(exc)])
        return

    result = actions.send_prompt(
        tower,
        pane_identity.pane_key,
        message,
        expected_project=chosen.get("project"),
        expected_agent=str(chosen.get("agent") or ""),
        expected_identity=identity,
        require_new_prompt=True,
    )
    if result.ok and hasattr(tower, "overrides"):
        tower.overrides.set_role(pane_identity.pane_key, stage.role_id or "", pane_identity.session, pane_identity.pane_pid)
    detail = t("workflow.send_started") if result.ok and result.submitted else (
        t("workflow.send_uncertain") if result.ok else t("workflow.send_not_confirmed")
    )
    show_message_screen(stdscr, t("workflow.send_title"), [detail, t("workflow.record_outcome_manually")])


def _compatible_panes(tower, run: WorkflowRun) -> list[dict]:
    if run.is_remote or run.host_key != getattr(tower, "local_host", ""):
        return []
    rows = list(getattr(tower, "rows", []))
    return [row for row in rows if _pane_in_workspace(row, run)]


def _pane_in_workspace(row: dict, run: WorkflowRun) -> bool:
    if (
        row.get("kind") != "pane"
        or not _pane_workspace_matches(row, run)
        or row.get("status") == "DEAD"
        or row.get("placeholder")
        or not all(row.get(field) for field in ("key", "pane_id", "session", "pane_pid"))
        or row.get("auto_agent") not in run.agents
        or row.get("auto_agent") == "Shell"
        or row.get("attention") not in (None, "", "none")
        or row.get("interaction") is not None
    ):
        return False
    return True


def _pane_workspace_matches(row: dict, run: WorkflowRun) -> bool:
    if (
        row.get("remote")
        or row.get("transport") != "local"
        or row.get("execution_host") != run.host_key
        or not row.get("path")
    ):
        return False
    try:
        pane_path = Path(str(row.get("path") or "")).expanduser().resolve(strict=False)
        roots = [Path(ref.path).expanduser().resolve(strict=False) for ref in run.workspaces]
    except (OSError, RuntimeError, ValueError):
        return False
    return any(pane_path == root or root in pane_path.parents for root in roots)


def _same_pane(current: dict, selected: dict, run: WorkflowRun) -> bool:
    fields = (
        "key", "pane_id", "session", "pane_pid", "agent", "auto_agent", "task_name", "project", "path", "transport"
    )
    if not _pane_in_workspace(current, run):
        return False
    return all(str(current.get(field) or "") == str(selected.get(field) or "") for field in fields) and not current.get("remote")


def _pane_identity_fields(row: dict) -> dict:
    return {
        "pane_id": row.get("pane_id") or "",
        "session": row.get("session") or "",
        "pane_pid": row.get("pane_pid") or "",
        "host": row.get("execution_host") or "",
        "transport": row.get("transport") or "",
        "path": row.get("path") or "",
        "provider": row.get("auto_agent") or "",
        "task_label": row.get("task_name") or "",
        "project": row.get("project") or "",
    }


def _compose_prompt(role_id: str, harness: str, brief: str) -> str:
    return f"## Tower role: {role_id}\n{harness.strip()}\n\n## Task brief\n{brief.strip()}"


def _show_harness_preview(stdscr, role_id: str, harness: str) -> bool:
    lines = [f'{t("workflow.harness_role", role=t(f"role.{role_id}"))}', "", *harness.splitlines()]
    return _scroll_preview(stdscr, t("workflow.harness_title"), lines, t("workflow.harness_hint"))


def _show_prompt_preview(stdscr, role_id: str, provider: str, task_label: str, row: dict, message: str) -> bool:
    lines = [
        t("workflow.role_line", role=t(f"role.{role_id}")),
        t("workflow.provider_line", provider=provider),
        t("workflow.task_line", task=task_label),
        f'{t("detail.pane_id")}  {row.get("pane_id") or row.get("key")}',
        "",
        *message.splitlines(),
    ]
    return _scroll_preview(stdscr, t("workflow.prompt_preview_title"), lines, t("workflow.prompt_preview_hint"))


def _scroll_preview(stdscr, title: str, lines: list[str], hint: str) -> bool:
    top = 0
    height = stdscr.getmaxyx()[0]
    while True:
        stdscr.erase()
        safe_add(stdscr, 0, 2, title, curses.A_BOLD)
        visible = max(1, height - 2)
        for offset, line in enumerate(lines[top : top + visible], start=1):
            safe_add(stdscr, offset, 2, line)
        safe_add(stdscr, height - 1, 2, hint, curses.A_DIM)
        stdscr.refresh()
        key = read_key(stdscr)
        if is_escape(key) or key in ("q", "Q"):
            return False
        if is_enter(key):
            return True
        if key in (curses.KEY_DOWN, curses.KEY_NPAGE, "j"):
            top = min(max(0, len(lines) - visible), top + (visible if key == curses.KEY_NPAGE else 1))
        elif key in (curses.KEY_UP, curses.KEY_PPAGE, "k"):
            top = max(0, top - (visible if key == curses.KEY_PPAGE else 1))


def _preview_template(stdscr, preset: WorkflowPreset) -> None:
    show_message_screen(
        stdscr,
        t("workflow.preview_title", name=preset.name),
        [t("workflow.preview_only"), "", *_stage_labels(preset)],
    )


def _stage_labels(preset: WorkflowPreset) -> list[str]:
    return [
        f'{index}. {t(f"role.{stage.role_id}") if isinstance(stage, RoleStage) else t("workflow.human_gate")}'
        for index, stage in enumerate(preset.stages, start=1)
    ]


def _workspace_label(workspace) -> str:
    roots = ", ".join(ref.name for ref in workspace.workspaces)
    return f'{workspace.name} · {workspace.host_label} · {roots} · {", ".join(workspace.agents)}'


def _workspace_label_from_run(run: WorkflowRun) -> str:
    roots = ", ".join(ref.name for ref in run.workspaces)
    return f'{run.workspace_name} · {run.host_label} · {roots}'


def _pane_label(row: dict) -> str:
    task = row.get("task_name") or row.get("project") or t("project.no_name")
    return f'{task} · {row.get("auto_agent") or "-"} · {row.get("pane_id") or row.get("key")}'


def _run_label(run: WorkflowRun) -> str:
    index = _current_stage_index(run)
    if index is None:
        state = t("workflow.done")
    else:
        stage = run.stages[index]
        role = t(f"role.{stage.role_id}") if stage.kind == "role" else t("workflow.human_gate")
        state = f'{role} · {t("workflow.state." + stage.state)}'
    return f'{run.workspace_name} · {run.workflow_name} · {state} · {run.run_id[:8]}'


def _current_stage_index(run: WorkflowRun):
    return next((index for index, stage in enumerate(run.stages) if stage.state != "PASS"), None)


def _run_lines(run: WorkflowRun, current_index=None) -> list[str]:
    lines = [
        t("workflow.workspace_line", workspace=_workspace_label_from_run(run)),
        t("workflow.run_id", run_id=run.run_id[:12]),
        t("workflow.order_line", name=run.workflow_name),
    ]
    for index, stage in enumerate(run.stages):
        label = t(f"role.{stage.role_id}") if stage.kind == "role" else t("workflow.human_gate")
        marker = "› " if index == current_index else "  "
        lines.append(f'{marker}{index + 1}. {label} · {t("workflow.state." + stage.state)}')
        if stage.kind == "role" and (stage.provider or stage.task_label):
            lines.append(f'   {stage.provider or "-"} · {stage.task_label or "-"}')
    return lines


def _running_target_lines(tower, run: WorkflowRun, stage: WorkflowStageRun) -> list[str]:
    if stage.kind == "human_gate":
        return [t("workflow.human_no_dispatch")]
    pane = stage.pane
    if pane is None:
        return [t("workflow.target_missing")]
    tower.load()
    row = next((item for item in tower.rows if item.get("key") == pane.pane_key), None)
    if (
        row is None
        or row.get("pane_id") != pane.pane_key
        or row.get("session") != pane.session
        or str(row.get("pane_pid") or "") != pane.pane_pid
        or row.get("auto_agent") != stage.provider
        or row.get("status") == "DEAD"
        or not _pane_workspace_matches(row, run)
    ):
        return [t("workflow.target_missing"), t("workflow.target_recoverable")]
    return [t("workflow.target_verified", pane=pane.pane_key)]
