import json
from types import SimpleNamespace

import pytest

from tmux_agent_tower.control import actions
from tmux_agent_tower.launcher.plan import WorkspaceRef
from tmux_agent_tower.launcher.presets import new_preset
from tmux_agent_tower.launcher.workflows import HumanGateStage, RoleStage, WorkflowPreset, WORKFLOW_PRESETS
from tmux_agent_tower.state.workflow_runs import (
    PaneIdentity,
    STAGE_STATES,
    WorkflowRunError,
    WorkflowRunStore,
)
from tmux_agent_tower.ui import workflow_presets
from tmux_agent_tower.ui.widgets import PickResult


def _workspace(root, *, agents=("Codex",)):
    return new_preset(
        name="repo-work",
        preset_id="workspace-1",
        host_key="workstation-a",
        host_label="workstation-a",
        is_remote=False,
        workspaces=(WorkspaceRef("repo", str(root)),),
        agents=tuple(agents),
        layout="tiled",
    )


def _workflow(stages=None):
    return WorkflowPreset("test-order", "시험 순서", tuple(stages or (RoleStage("builder"), RoleStage("qa"))))


def _store(tmp_path, stages=None):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    store = WorkflowRunStore(tmp_path / "workflow-runs.json")
    run = store.create(_workspace(root), _workflow(stages), run_id="run-1")
    return root, store, run


def _start(store, run_id, index, pane_id="%31", provider="Codex", task="repo"):
    return store.start_stage(
        run_id,
        index,
        pane=PaneIdentity(pane_id, "main", pane_id.strip("%") + "0"),
        provider=provider,
        task_label=task,
    )


def test_run_transitions_are_explicit_and_next_stage_stays_pending(tmp_path):
    _root, store, run = _store(tmp_path)
    assert [stage.state for stage in run.stages] == ["PENDING", "PENDING"]
    assert set(STAGE_STATES) == {"PENDING", "RUNNING", "PASS", "FAIL", "BLOCKED"}

    started = _start(store, run.run_id, 0)
    assert started.stages[0].state == "RUNNING"
    assert started.stages[1].state == "PENDING"
    with pytest.raises(WorkflowRunError, match="실행 중인"):
        _start(store, run.run_id, 1, "%32")

    passed = store.finish_stage(run.run_id, 0, "PASS")
    assert passed.stages[0].state == "PASS"
    assert passed.stages[1].state == "PENDING"  # PASS never relays or starts the next stage.
    assert passed.stages[0].finished_at

    second = _start(store, run.run_id, 1, "%32", task="review")
    blocked = store.finish_stage(run.run_id, 1, "BLOCKED")
    assert blocked.stages[1].state == "BLOCKED"
    reset = store.retry_stage(run.run_id, 1)
    assert reset.stages[1].state == "PENDING"
    assert reset.stages[1].pane is None
    assert reset.stages[1].started_at == reset.stages[1].finished_at == ""
    assert reset.stages[0].state == "PASS"
    assert second.stages[1].state == "RUNNING"


def test_invalid_transitions_and_human_gate_agent_fields_are_refused(tmp_path):
    _root, store, run = _store(tmp_path, (RoleStage("builder"), HumanGateStage()))
    with pytest.raises(WorkflowRunError, match="pane 신원"):
        store.start_stage(run.run_id, 0)
    with pytest.raises(WorkflowRunError, match="앞 단계"):
        _start(store, run.run_id, 1)
    with pytest.raises(WorkflowRunError, match="실행 중인"):
        store.finish_stage(run.run_id, 0, "PASS")
    with pytest.raises(WorkflowRunError, match="PASS, FAIL, BLOCKED"):
        store.finish_stage(run.run_id, 0, "RUNNING")

    _start(store, run.run_id, 0)
    store.finish_stage(run.run_id, 0, "PASS")
    with pytest.raises(WorkflowRunError, match="사람 확인 단계"):
        store.start_stage(run.run_id, 1, pane=PaneIdentity("%99", "main", "990"), provider="Codex", task_label="repo")
    gate = store.start_stage(run.run_id, 1)
    assert gate.stages[1].state == "RUNNING"
    assert gate.stages[1].pane is None and gate.stages[1].provider == gate.stages[1].task_label == ""
    assert store.finish_stage(run.run_id, 1, "PASS").stages[1].state == "PASS"


def test_human_gate_persistence_has_no_role_provider_pane_or_prompt_fields(tmp_path):
    _root, store, run = _store(tmp_path, (RoleStage("builder"), HumanGateStage()))
    _start(store, run.run_id, 0)
    store.finish_stage(run.run_id, 0, "PASS")
    gate = store.start_stage(run.run_id, 1)
    document = json.loads((tmp_path / "workflow-runs.json").read_text(encoding="utf-8"))

    assert gate.stages[1].kind == "human_gate"
    assert set(document["runs"][0]["stages"][1]) == {"kind", "state", "started_at", "finished_at"}


def test_atomic_store_survives_restart_and_only_persists_allowlisted_metadata(tmp_path):
    _root, store, run = _store(tmp_path)
    started = _start(store, run.run_id, 0, task="로그인 작업")
    path = tmp_path / "workflow-runs.json"
    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw)

    assert WorkflowRunStore(path).get(run.run_id) == started
    assert document["runs"][0]["stages"][0]["pane"] == {
        "pane_key": "%31", "session": "main", "pane_pid": "310"
    }
    assert set(document["runs"][0]["stages"][0]) == {
        "kind", "role_id", "state", "started_at", "finished_at", "pane", "provider", "task_label"
    }
    assert "prompt" not in raw.lower() and "secret" not in raw.lower() and "result body" not in raw.lower()
    assert "\"workflow_id\"" not in raw  # workflow reference has an explicit, smaller schema.
    assert path.stat().st_mode & 0o777 == 0o600


def _pane(root, key, *, pid=None, agent="Codex", task="task-a", project="repo", **updates):
    row = {
        "kind": "pane",
        "key": key,
        "pane_id": key,
        "session": "main",
        "pane_pid": pid or key.strip("%") + "0",
        "agent": agent,
        "auto_agent": agent,
        "task_name": task,
        "project": project,
        "path": str(root),
        "status": "IDLE",
        "remote": False,
        "transport": "local",
        "execution_host": "workstation-a",
    }
    row.update(updates)
    return row


def test_create_run_chooses_workspace_and_workflow_separately_and_starts_pending(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    preset = _workspace(root)
    store = WorkflowRunStore(tmp_path / "workflow-runs.json")
    choices = iter(("workspace-1", "quick-fix", "create"))
    confirmations = []
    monkeypatch.setattr(
        workflow_presets,
        "WorkspacePresetStore",
        lambda: SimpleNamespace(list=lambda: [preset]),
    )
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _s, title, items, **kwargs: confirmations.append((title, kwargs.get("preamble")))
        or PickResult(selected_key=next(choices)),
    )
    monkeypatch.setattr(workflow_presets, "show_message_screen", lambda *_: None)
    opened = []
    monkeypatch.setattr(
        workflow_presets,
        "_open_run_detail",
        lambda _s, _tower, _store, run_id: opened.append(run_id),
    )

    workflow_presets._create_run(None, _Tower([]), store)

    run = store.get(opened[0])
    assert run.workspace_id == preset.preset_id
    assert run.workflow_id == "quick-fix"
    assert [stage.state for stage in run.stages] == ["PENDING", "PENDING", "PENDING"]
    assert len(confirmations) == 3


def test_create_run_persists_selected_custom_workflow_as_pending_without_dispatch(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    preset = _workspace(root)
    custom = WorkflowPreset(
        "custom-review", "사용자 검토", (RoleStage("reviewer"), HumanGateStage(), RoleStage("qa"))
    )
    store = WorkflowRunStore(tmp_path / "workflow-runs.json")
    choices = iter(("workspace-1", "custom-review", "create"))
    seen = []
    opened = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("creating a workflow run must not dispatch")

    monkeypatch.setattr(
        workflow_presets,
        "WorkspacePresetStore",
        lambda: SimpleNamespace(list=lambda: [preset]),
    )
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _s, title, items, **kwargs: seen.append((title, items, kwargs.get("preamble")))
        or PickResult(selected_key=next(choices)),
    )
    monkeypatch.setattr(workflow_presets, "show_message_screen", lambda *_: None)
    monkeypatch.setattr(workflow_presets, "_open_run_detail", lambda _s, _tower, _store, run_id: opened.append(run_id))
    monkeypatch.setattr(actions, "send_prompt", forbidden)
    monkeypatch.setattr("tmux_agent_tower.launcher.plan.execute_launch_plan", forbidden)

    workflow_presets._create_run(None, _Tower([]), store, (custom,))

    run = store.get(opened[0])
    assert run.workflow_id == "custom-review"
    assert run.workflow_name == "사용자 검토"
    assert [stage.kind for stage in run.stages] == ["role", "human_gate", "role"]
    assert [stage.state for stage in run.stages] == ["PENDING"] * 3
    assert any(("custom-review", "사용자 검토") in items for _title, items, _preamble in seen)
    assert [stage["state"] for stage in json.loads(store.path.read_text(encoding="utf-8"))["runs"][-1]["stages"]] == ["PENDING"] * 3


class _Tower:
    def __init__(self, rows, *, local_host="workstation-a", on_load=None):
        self.rows = rows
        self.local_host = local_host
        self.overrides = SimpleNamespace(set_role=lambda *args: self.role_sets.append(args))
        self.role_sets = []
        self.loads = 0
        self.on_load = on_load

    def load(self):
        self.loads += 1
        if self.on_load:
            self.rows = self.on_load(self.loads, self.rows)


def test_agent_stage_sends_only_to_the_confirmed_pane_and_keeps_brief_ephemeral(tmp_path, monkeypatch):
    root, store, run = _store(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    rows = [
        _pane(root, "%11", task="task-one"),
        _pane(root, "%22", pid="220", task="selected-task"),
        _pane(root, "%33", agent="Claude", task="other-provider"),
        _pane(other_root, "%44", task="other-workspace"),
        _pane(root, "%55", task="shell-with-label-override", auto_agent="Shell"),
        _pane(root, "%66", task="approval-wait", attention="approval_required", interaction={"type": "approval"}),
        _pane(root, "%77", task="remote-task", remote=True, transport="local"),
    ]
    tower = _Tower(rows)
    picks = iter(("%22", "send"))
    menus = []
    sent = []
    harnesses = []
    previews = []
    monkeypatch.setattr(workflow_presets, "load_harness", lambda role: harnesses.append(role) or "HARNESS PRIVATE CONTENT")
    monkeypatch.setattr(workflow_presets, "_show_harness_preview", lambda *_: True)
    monkeypatch.setattr(workflow_presets, "prompt_composer", lambda *_args, **_kwargs: "brief PRIVATE CONTENT")
    monkeypatch.setattr(
        workflow_presets,
        "_show_prompt_preview",
        lambda *args: previews.append(args) or True,
    )
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _s, title, items, **kwargs: menus.append((title, list(items), kwargs.get("preamble")))
        or PickResult(selected_key=next(picks)),
    )
    monkeypatch.setattr(
        workflow_presets.actions,
        "send_prompt",
        lambda *args, **kwargs: sent.append((args, kwargs)) or SimpleNamespace(ok=True, submitted=True),
    )
    monkeypatch.setattr(workflow_presets, "show_message_screen", lambda *_: None)

    workflow_presets._start_role_stage(None, tower, store, run, 0, run.stages[0])

    refreshed = store.get(run.run_id)
    stage = refreshed.stages[0]
    assert stage.state == "RUNNING" and stage.role_id == "builder"
    assert stage.provider == "Codex" and stage.task_label == "selected-task"
    assert stage.pane == PaneIdentity("%22", "main", "220")
    assert refreshed.stages[1].state == "PENDING"
    assert harnesses == ["builder"]  # role is independent from the selected Agent/provider.
    assert "brief PRIVATE CONTENT" in previews[0][-1]
    candidate_keys = [key for key, _label in menus[0][1]]
    assert candidate_keys == ["%11", "%22", "cancel"]
    assert len(sent) == 1 and sent[0][0][1] == "%22"
    assert "## Tower role: builder" in sent[0][0][2] and "brief PRIVATE CONTENT" in sent[0][0][2]
    assert sent[0][1]["expected_identity"] == {
        "pane_id": "%22", "session": "main", "pane_pid": "220", "host": "workstation-a",
        "transport": "local", "path": str(root), "provider": "Codex",
        "task_label": "selected-task", "project": "repo",
    }
    assert sent[0][1]["require_new_prompt"] is True
    assert tower.role_sets == [("%22", "builder", "main", "220")]
    assert "%11" not in str(sent) and "%33" not in str(sent) and "%44" not in str(sent)
    persisted = (tmp_path / "workflow-runs.json").read_text(encoding="utf-8")
    assert "PRIVATE CONTENT" not in persisted and "HARNESS" not in persisted


def test_agent_stage_requires_final_confirmation_and_never_auto_advances(tmp_path, monkeypatch):
    root, store, run = _store(tmp_path)
    tower = _Tower([_pane(root, "%11")])
    picks = iter(("%11", "cancel"))
    monkeypatch.setattr(workflow_presets, "load_harness", lambda _role: "harness")
    monkeypatch.setattr(workflow_presets, "_show_harness_preview", lambda *_: True)
    monkeypatch.setattr(workflow_presets, "prompt_composer", lambda *_args, **_kwargs: "brief")
    monkeypatch.setattr(workflow_presets, "_show_prompt_preview", lambda *_args: True)
    monkeypatch.setattr(workflow_presets, "run_list_picker", lambda *_a, **_k: PickResult(selected_key=next(picks)))
    monkeypatch.setattr(workflow_presets.actions, "send_prompt", lambda *_a, **_k: pytest.fail("must require confirmation"))

    workflow_presets._start_role_stage(None, tower, store, run, 0, run.stages[0])

    persisted = store.get(run.run_id)
    assert [stage.state for stage in persisted.stages] == ["PENDING", "PENDING"]
    assert tower.role_sets == []


def test_reused_stale_pane_fails_closed_before_metadata_or_send(tmp_path, monkeypatch):
    root, store, run = _store(tmp_path)
    initial = [_pane(root, "%11", pid="110")]

    def refresh(load_count, rows):
        return [_pane(root, "%11", pid="999")] if load_count >= 2 else rows

    tower = _Tower(initial, on_load=refresh)
    picks = iter(("%11", "send"))
    messages = []
    monkeypatch.setattr(workflow_presets, "load_harness", lambda _role: "harness")
    monkeypatch.setattr(workflow_presets, "_show_harness_preview", lambda *_: True)
    monkeypatch.setattr(workflow_presets, "prompt_composer", lambda *_args, **_kwargs: "brief")
    monkeypatch.setattr(workflow_presets, "_show_prompt_preview", lambda *_args: True)
    monkeypatch.setattr(workflow_presets, "run_list_picker", lambda *_a, **_k: PickResult(selected_key=next(picks)))
    monkeypatch.setattr(workflow_presets.actions, "send_prompt", lambda *_a, **_k: pytest.fail("stale pane sent"))
    monkeypatch.setattr(workflow_presets, "show_message_screen", lambda _s, title, lines: messages.append((title, lines)))

    workflow_presets._start_role_stage(None, tower, store, run, 0, run.stages[0])

    assert store.get(run.run_id).stages[0].state == "PENDING"
    assert tower.role_sets == []
    assert any("바뀌었습니다" in title for title, _ in messages)


def test_human_gate_has_no_harness_or_dispatch_and_never_auto_passes(tmp_path, monkeypatch):
    _root, store, run = _store(tmp_path, (RoleStage("builder"), RoleStage("qa"), HumanGateStage()))
    _start(store, run.run_id, 0)
    store.finish_stage(run.run_id, 0, "PASS")
    _start(store, run.run_id, 1, "%32", task="review")
    store.finish_stage(run.run_id, 1, "PASS")
    picks = iter(("start", "BLOCKED", "back"))
    menu_keys = []
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _s, _title, items, **_kwargs: menu_keys.append([key for key, _ in items])
        or PickResult(selected_key=next(picks)),
    )
    monkeypatch.setattr(workflow_presets, "load_harness", lambda *_: pytest.fail("human gate loaded a Harness"))
    monkeypatch.setattr(workflow_presets.actions, "send_prompt", lambda *_a, **_k: pytest.fail("human gate dispatched"))
    monkeypatch.setattr(workflow_presets, "show_message_screen", lambda *_: None)

    workflow_presets._open_run_detail(None, _Tower([]), store, run.run_id)

    gate = store.get(run.run_id).stages[2]
    assert gate.kind == "human_gate" and gate.state == "BLOCKED"
    assert gate.pane is None and gate.provider == gate.task_label == ""
    assert menu_keys[0] == ["start", "back"]
    assert "PASS" in menu_keys[1]


def test_run_detail_exposes_next_stage_as_separate_human_action(tmp_path, monkeypatch):
    _root, store, run = _store(tmp_path)
    _start(store, run.run_id, 0)
    store.finish_stage(run.run_id, 0, "PASS")
    captured = {}
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _s, _title, items, **_kwargs: captured.update(items=items) or PickResult(selected_key="back"),
    )

    workflow_presets._open_run_detail(None, _Tower([]), store, run.run_id)

    assert dict(captured["items"])["start"] == "다음 단계 시작"
    assert store.get(run.run_id).stages[1].state == "PENDING"


def test_fail_requires_explicit_retry_and_leaves_next_stage_pending(tmp_path, monkeypatch):
    _root, store, run = _store(tmp_path)
    _start(store, run.run_id, 0)
    failed = store.finish_stage(run.run_id, 0, "FAIL")
    menus = []
    picks = iter(("retry", "back"))
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _s, _title, items, **_kwargs: menus.append(items)
        or PickResult(selected_key=next(picks)),
    )

    workflow_presets._open_run_detail(None, _Tower([]), store, run.run_id)

    retried = store.get(run.run_id)
    assert failed.stages[0].state == "FAIL"
    assert [key for key, _label in menus[0]] == ["retry", "back"]
    assert [stage.state for stage in retried.stages] == ["PENDING", "PENDING"]
    assert [key for key, _label in menus[1]] == ["start", "back"]


def test_safe_send_target_guard_refuses_pid_or_host_reuse(tmp_path):
    class FreshTower:
        def load(self):
            self.rows = [{
                "key": "%11", "pane_id": "%11", "session": "main", "pane_pid": "999",
                "project": "repo", "agent": "Codex", "execution_host": "OTHER", "transport": "local",
                "path": str(tmp_path), "remote": False,
            }]

    tower = FreshTower()
    ok, reason, pane_id = actions.find_prompt_target(
        tower,
        "%11",
        "repo",
        "Codex",
        {"pane_id": "%11", "session": "main", "pane_pid": "110", "host": "workstation-a", "transport": "local", "path": str(tmp_path)},
    )
    assert (ok, reason, pane_id) == (False, "mismatch", None)


def test_safe_send_target_guard_refuses_active_interaction(tmp_path):
    class WaitingTower:
        def load(self):
            self.rows = [{
                "key": "%11", "pane_id": "%11", "session": "main", "pane_pid": "110",
                "project": "repo", "agent": "Codex", "execution_host": "workstation-a", "transport": "local",
                "path": str(tmp_path), "remote": False, "attention": "approval_required",
                "interaction": {"type": "approval", "options": [{"key": "y", "safe": True}]},
            }]

    ok, reason, pane_id = actions.find_prompt_target(
        WaitingTower(), "%11", "repo", "Codex", require_new_prompt=True
    )
    assert (ok, reason, pane_id) == (False, "interaction_unknown", None)


def test_missing_running_target_is_recoverable_and_does_not_change_stage_state(tmp_path):
    _root, store, run = _store(tmp_path)
    _start(store, run.run_id, 0)
    tower = _Tower([])

    lines = workflow_presets._running_target_lines(tower, run, store.get(run.run_id).stages[0])

    assert lines[0] == "저장된 pane 신원을 현재 확인할 수 없습니다."
    assert store.get(run.run_id).stages[0].state == "RUNNING"


def test_remote_workspace_agent_stage_stays_pending_without_ssh_dispatch(tmp_path, monkeypatch):
    workspace = new_preset(
        name="remote-work",
        preset_id="remote-workspace",
        host_key="workstation-b",
        host_label="workstation-b",
        is_remote=True,
        workspaces=(WorkspaceRef("repo", "/work/repo"),),
        agents=("Codex",),
        layout="tiled",
    )
    store = WorkflowRunStore(tmp_path / "workflow-runs.json")
    run = store.create(workspace, _workflow((RoleStage("builder"),)), run_id="remote-run")
    tower = _Tower([])
    shown = []
    monkeypatch.setattr(workflow_presets, "load_harness", lambda _role: "harness")
    monkeypatch.setattr(workflow_presets, "_show_harness_preview", lambda *_: True)
    monkeypatch.setattr(workflow_presets, "show_message_screen", lambda _s, _t, lines: shown.extend(lines))
    monkeypatch.setattr(workflow_presets.actions, "send_prompt", lambda *_a, **_k: pytest.fail("remote dispatch is forbidden"))

    workflow_presets._start_role_stage(None, tower, store, run, 0, run.stages[0])

    assert store.get(run.run_id).stages[0].state == "PENDING"
    assert any("로컬 pane만 지원" in line for line in shown)


def test_human_gate_definition_stays_provider_and_pane_free():
    gate = WORKFLOW_PRESETS[0].stages[-1]
    assert isinstance(gate, HumanGateStage)
    assert not isinstance(gate, RoleStage)
    assert not hasattr(gate, "role_id") and not hasattr(gate, "provider") and not hasattr(gate, "pane")
