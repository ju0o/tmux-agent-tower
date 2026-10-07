import json
from dataclasses import fields

import pytest

from tmux_agent_tower.i18n.ko import STRINGS
from tmux_agent_tower.launcher import plan, presets, spawn
from tmux_agent_tower.launcher.workflows import (
    HumanGateStage,
    RoleStage,
    WORKFLOW_PRESETS,
    WorkflowPreset,
    WorkflowPresetFileError,
    load_custom_workflows,
)
from tmux_agent_tower.ui import structure_menu, workflow_presets
from tmux_agent_tower.ui.widgets import PickResult


def _stage_ids(preset):
    return [stage.role_id if isinstance(stage, RoleStage) else "human_gate" for stage in preset.stages]


def test_default_workflows_have_the_exact_ordered_role_sequences():
    assert [(preset.preset_id, preset.name) for preset in WORKFLOW_PRESETS] == [
        ("quick-fix", "빠른 수정"),
        ("feature-development", "기능 개발"),
        ("focused-improvement", "집중 고도화"),
    ]
    assert [_stage_ids(preset) for preset in WORKFLOW_PRESETS] == [
        ["builder", "qa", "human_gate"],
        ["planner", "builder", "reviewer", "qa"],
        ["orchestrator", "planner", "builder", "qa", "dogfood", "e2e", "human_gate"],
    ]


def test_human_gate_is_a_distinct_human_owned_stage_and_agent_names_are_not_roles():
    gate = WORKFLOW_PRESETS[0].stages[-1]
    assert isinstance(gate, HumanGateStage)
    assert not hasattr(gate, "role_id")
    assert not hasattr(gate, "agent")
    assert not hasattr(gate, "command")
    assert fields(HumanGateStage) == ()
    assert STRINGS["workflow.human_gate"] == "사람 확인"
    assert STRINGS["role.builder"] == "구현"
    with pytest.raises(ValueError, match="작업 역할"):
        RoleStage("Codex")


def test_public_custom_workflow_model_accepts_known_stages_and_rejects_unsafe_definitions():
    custom = WorkflowPreset(
        "custom-order",
        "내 작업 순서",
        (RoleStage("planner"), HumanGateStage(), RoleStage("builder")),
    )
    assert _stage_ids(custom) == ["planner", "human_gate", "builder"]

    with pytest.raises(ValueError, match="중복"):
        WorkflowPreset("duplicate-role", "중복 역할", (RoleStage("qa"), RoleStage("qa")))
    with pytest.raises(ValueError, match="중복"):
        WorkflowPreset("duplicate-gate", "중복 사람 확인", (HumanGateStage(), HumanGateStage()))
    with pytest.raises(ValueError, match="작업 역할"):
        RoleStage("unknown")
    with pytest.raises(ValueError, match="역할 또는 사람 확인"):
        WorkflowPreset("unknown-stage", "미지원 단계", (object(),))
    with pytest.raises(ValueError, match="단계가 필요"):
        WorkflowPreset("mutable-stages", "변경 가능한 단계", [RoleStage("builder")])


def test_custom_workflow_json_loads_only_ordered_known_roles_and_explicit_human_gate(tmp_path):
    path = tmp_path / "workflow-presets.json"
    path.write_text(
        json.dumps({"version": 1, "workflows": [{
            "id": "custom-review",
            "name": "사용자 검토",
            "stages": ["planner", "builder", "human_gate", "qa"],
        }]}),
        encoding="utf-8",
    )

    (custom,) = load_custom_workflows(path)

    assert custom.preset_id == "custom-review"
    assert _stage_ids(custom) == ["planner", "builder", "human_gate", "qa"]
    assert {field.name for field in fields(custom)} == {"preset_id", "name", "stages"}


@pytest.mark.parametrize(
    "content",
    [
        "{invalid json",
        '{"version": true, "workflows": []}',
        '{"version": 1, "workflows": [{"id": "bad/id", "name": "잘못된 ID", "stages": ["qa"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "중복 역할", "stages": ["qa", "qa"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "중복 사람 확인", "stages": ["qa", "human_gate", "human_gate"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "미지원 역할", "stages": ["Codex"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "스크립트", "stages": ["qa"], "script": "echo unsafe"}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "명령", "stages": ["qa"], "command": "echo unsafe"}]}',
        '{"version": 1, "workflows": [{"id": "quick-fix", "name": "중복 ID", "stages": ["qa"]}]}',
        '{"version": 1, "workflows": [{"id": "one", "name": "한 순서", "stages": ["qa"]}, {"id": "one", "name": "다른 순서", "stages": ["builder"]}]}',
        '{"version": 1, "workflows": [{"id": "one", "name": "같은 이름", "stages": ["qa"]}, {"id": "two", "name": "같은 이름", "stages": ["builder"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": 1, "stages": ["qa"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "잘못된 단계 자료형", "stages": [1]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "숨은 제어 문자\u202e", "stages": ["qa"]}]}',
        '{"version": 1, "workflows": [{"id": "custom", "name": "빈 단계", "stages": []}]}',
        '{"version": 1, "version": 1, "workflows": []}',
    ],
)
def test_invalid_custom_workflow_json_fails_closed(tmp_path, content):
    path = tmp_path / "workflow-presets.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(WorkflowPresetFileError):
        load_custom_workflows(path)


def test_oversized_custom_workflow_file_fails_closed(tmp_path):
    from tmux_agent_tower.launcher.workflows import MAX_CUSTOM_WORKFLOW_BYTES

    path = tmp_path / "workflow-presets.json"
    path.write_bytes(b" " * (MAX_CUSTOM_WORKFLOW_BYTES + 1))

    with pytest.raises(WorkflowPresetFileError):
        load_custom_workflows(path)


def test_workflow_definition_stays_out_of_workspace_preset_model_and_store(tmp_path):
    assert {field.name for field in fields(WorkflowPreset)} == {"preset_id", "name", "stages"}
    assert "stages" not in {field.name for field in fields(presets.WorkspacePreset)}
    assert not hasattr(presets, "WorkflowPresetStore")

    workspace = tmp_path / "repo"
    workspace.mkdir()
    workspace_preset = presets.new_preset(
        name="repo",
        host_key="workstation-a",
        host_label="workstation-a",
        is_remote=False,
        workspaces=(plan.WorkspaceRef("repo", str(workspace)),),
        agents=("Codex",),
        layout="tiled",
    )
    store_path = tmp_path / "workspace-presets.json"
    presets.WorkspacePresetStore(store_path).save(workspace_preset)
    persisted = json.loads(store_path.read_text(encoding="utf-8"))["presets"][0]
    assert set(persisted) == {
        "id", "name", "host_key", "host_label", "is_remote", "workspaces", "agents", "layout"
    }
    assert "workflow" not in persisted and "stages" not in persisted


def test_create_hub_exposes_a_separate_workflow_route(monkeypatch, tmp_path):
    from tmux_agent_tower.ui import tower as tower_ui

    seen = {}
    opened = []
    tower = object()
    monkeypatch.setattr(tower_ui, "STATE_DIR", tmp_path)
    picks = iter(("more", "workflow"))
    def pick(_screen, _title, items, **_kwargs):
        seen.setdefault("menus", []).append(dict(items))
        return PickResult(selected_key=next(picks))

    monkeypatch.setattr(structure_menu, "run_list_picker", pick)
    monkeypatch.setattr(
        workflow_presets,
        "open_workflow_presets",
        lambda screen, selected_tower, state_dir: opened.append((screen, selected_tower, state_dir)),
    )

    structure_menu.open_create_hub("fake-screen", tower)

    assert "workflow" not in seen["menus"][0]
    assert seen["menus"][1]["workflow"] == STRINGS["struct.create_workflow"] == "작업 순서 보기"
    assert opened == [("fake-screen", tower, tmp_path)]


def test_preview_displays_every_builtin_order_and_human_gate_without_dispatch(monkeypatch, tmp_path):
    calls = []
    picks = iter(tuple(f"template:{preset.preset_id}" for preset in WORKFLOW_PRESETS) + ("back",))
    previews = []

    def pick(_screen, _title, _items, **_kwargs):
        return PickResult(selected_key=next(picks))

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("preview must not launch")

    monkeypatch.setattr(workflow_presets, "run_list_picker", pick)
    monkeypatch.setattr(
        workflow_presets,
        "show_message_screen",
        lambda _screen, title, lines: previews.append((title, lines)),
    )
    monkeypatch.setattr(plan, "execute_launch_plan", forbidden)
    for name in ("spawn_local", "spawn_remote"):
        monkeypatch.setattr(plan, name, forbidden)
        monkeypatch.setattr(spawn, name, forbidden)

    workflow_presets.open_workflow_presets("fake-screen", object(), tmp_path)

    assert previews == [
        (
            "작업 순서 미리보기: 빠른 수정",
            [STRINGS["workflow.preview_only"], "", "1. 구현", "2. 확인", "3. 사람 확인"],
        ),
        (
            "작업 순서 미리보기: 기능 개발",
            [STRINGS["workflow.preview_only"], "", "1. 계획", "2. 구현", "3. 검수", "4. 확인"],
        ),
        (
            "작업 순서 미리보기: 집중 고도화",
            [
                STRINGS["workflow.preview_only"],
                "",
                "1. 조율",
                "2. 계획",
                "3. 구현",
                "4. 확인",
                "5. 실사용",
                "6. 전체 흐름",
                "7. 사람 확인",
            ],
        ),
    ]
    assert calls == []


def test_custom_workflow_is_previewable_with_ordered_roles_and_human_gate(monkeypatch, tmp_path):
    custom = WorkflowPreset(
        "custom-review", "사용자 검토", (RoleStage("reviewer"), HumanGateStage(), RoleStage("qa"))
    )
    monkeypatch.setattr(workflow_presets, "load_custom_workflows", lambda: (custom,))
    picks = iter(("template:custom-review", "back"))
    previews = []
    menu_items = []

    def pick(_screen, _title, items, **_kwargs):
        menu_items.append(items)
        return PickResult(selected_key=next(picks))

    monkeypatch.setattr(workflow_presets, "run_list_picker", pick)
    monkeypatch.setattr(
        workflow_presets,
        "show_message_screen",
        lambda _screen, title, lines: previews.append((title, lines)),
    )

    workflow_presets.open_workflow_presets("fake-screen", object(), tmp_path)

    assert ("template:custom-review", "순서 미리보기 · 사용자 검토") in menu_items[0]
    assert previews == [
        (
            "작업 순서 미리보기: 사용자 검토",
            [STRINGS["workflow.preview_only"], "", "1. 검수", "2. 사람 확인", "3. 확인"],
        )
    ]


def test_invalid_custom_file_shows_localized_error_and_keeps_builtin_choices(monkeypatch, tmp_path):
    from tmux_agent_tower.launcher.workflows import WorkflowPresetFileError

    menu_items = []
    messages = []
    monkeypatch.setattr(
        workflow_presets,
        "load_custom_workflows",
        lambda: (_ for _ in ()).throw(WorkflowPresetFileError("invalid")),
    )
    monkeypatch.setattr(
        workflow_presets,
        "run_list_picker",
        lambda _screen, _title, items, **_kwargs: menu_items.append(items) or PickResult(selected_key="back"),
    )
    monkeypatch.setattr(
        workflow_presets,
        "show_message_screen",
        lambda _screen, title, lines: messages.append((title, lines)),
    )

    workflow_presets.open_workflow_presets("fake-screen", object(), tmp_path)

    assert messages == [("단계별 작업", [STRINGS["workflow.custom_invalid"]])]
    assert all(f"template:{preset.preset_id}" in dict(menu_items[0]) for preset in WORKFLOW_PRESETS)
