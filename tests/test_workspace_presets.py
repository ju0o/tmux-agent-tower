import json
import stat

import pytest

from tmux_agent_tower.launcher import plan, presets
from tmux_agent_tower.launcher.browse import PathCheck
from tmux_agent_tower.launcher.discovery import ProjectEntry
from tmux_agent_tower.launcher.plan import LaunchPlan, LaunchPlanError, WorkspaceRef
from tmux_agent_tower.launcher.presets import PresetStoreError, WorkspacePresetStore, new_preset
from tmux_agent_tower.launcher.spawn import SpawnResult, SpawnTarget
from tmux_agent_tower.i18n import t
from tmux_agent_tower.ui import workspace_browser, workspace_presets
from tmux_agent_tower.ui.widgets import PickResult


def _preset(path, *, preset_id="first", name="리뷰", layout="main-vertical"):
    return new_preset(
        preset_id=preset_id,
        name=name,
        host_key="workstation-a",
        host_label="workstation-a",
        is_remote=False,
        workspaces=(WorkspaceRef("repo", str(path)),),
        agents=("Codex", "Claude"),
        layout=layout,
    )


def test_preset_crud_preserves_unrelated_document_fields_and_only_selected_item(tmp_path):
    path = tmp_path / "workspace-presets.json"
    path.write_text(json.dumps({"preferences": {"theme": "dark"}, "presets": []}), encoding="utf-8")
    store = WorkspacePresetStore(path)
    first = _preset(tmp_path, preset_id="first")
    second = _preset(tmp_path, preset_id="second", name="테스트")

    store.save(first)
    store.save(second)
    edited = _preset(tmp_path, preset_id="first", name="코드 리뷰")
    store.save(edited)
    store.delete("second")

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["preferences"] == {"theme": "dark"}
    assert [item["id"] for item in document["presets"]] == ["first"]
    assert store.list() == [edited]
    assert set(document["presets"][0]) == {
        "id", "name", "host_key", "host_label", "is_remote", "workspaces", "agents", "layout"
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_legacy_store_upgrades_and_malformed_store_is_not_overwritten(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"preferences": {"language": "ko"}, "presets": []}), encoding="utf-8")
    store = WorkspacePresetStore(legacy)
    store.save(_preset(tmp_path))
    saved = json.loads(legacy.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["preferences"] == {"language": "ko"}

    malformed = tmp_path / "bad.json"
    original = "{ definitely not json"
    malformed.write_text(original, encoding="utf-8")
    broken = WorkspacePresetStore(malformed)
    with pytest.raises(PresetStoreError):
        broken.list()
    with pytest.raises(PresetStoreError):
        broken.save(_preset(tmp_path))
    assert malformed.read_text(encoding="utf-8") == original


def test_saved_workspace_without_role_fields_still_loads_as_a_workspace_preset(tmp_path):
    path = tmp_path / "older-presets.json"
    path.write_text(json.dumps({"version": 1, "presets": [{
        "id": "old", "name": "기존 작업공간", "host_key": "workstation-a", "host_label": "workstation-a",
        "is_remote": False, "workspaces": [{"name": "repo", "path": str(tmp_path)}],
        "agents": ["Codex"], "layout": "tiled",
    }]}), encoding="utf-8")

    loaded = WorkspacePresetStore(path).list()
    assert len(loaded) == 1
    assert loaded[0].agents == ("Codex",)
    assert loaded[0].launch_plan().agents == ("Codex",)
    assert not hasattr(loaded[0], "role")


def test_atomic_write_failure_keeps_old_file_and_removes_own_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "workspace-presets.json"
    store = WorkspacePresetStore(path)
    store.save(_preset(tmp_path))
    before = path.read_bytes()

    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(presets.os, "replace", fail_replace)
    with pytest.raises(PresetStoreError):
        store.save(_preset(tmp_path, preset_id="second", name="두 번째"))

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".workspace-presets.json.*.tmp")) == []


def test_malformed_existing_app_config_is_left_untouched_by_preset_storage(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher import config

    config_path = tmp_path / "config.toml"
    old_config = 'language = "ko"\n[agents\nnot valid'
    config_path.write_text(old_config, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_path)
    assert config.load_config()["agents"]["Codex"] == "codex"

    WorkspacePresetStore(tmp_path / "workspace-presets.json").save(_preset(tmp_path))
    assert config_path.read_text(encoding="utf-8") == old_config


def test_launch_executor_uses_all_selected_agents_and_actual_layout_without_providers(tmp_path, monkeypatch):
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    launches = {}
    monkeypatch.setattr(plan, "spawn_local", lambda session, targets, agents_cfg, layout, **kwargs: launches.update(
        session=session, targets=targets, agents_cfg=agents_cfg, layout=layout
    ) or [SpawnResult(target, True, "fake") for target in targets])

    launch = LaunchPlan(
        host_key="workstation-a",
        host_label="workstation-a",
        is_remote=False,
        workspaces=(WorkspaceRef("one", str(first)), WorkspaceRef("two", str(second))),
        agents=("Codex", "Claude"),
        layout="main-vertical",
    )
    results = plan.execute_launch_plan(
        launch,
        session="tower-test",
        state_dir=tmp_path / "state",
        agents_cfg={"Codex": "fake-codex", "Claude": "fake-claude"},
    )

    assert [(row.project_path, row.agent_label) for row in launches["targets"]] == [
        (str(first), "Codex"), (str(first), "Claude"),
        (str(second), "Codex"), (str(second), "Claude"),
    ]
    assert launches["layout"] == "main-vertical"
    assert len(results) == 4


def test_plan_rejects_unsupported_layout_before_creating_any_resource(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(plan, "spawn_local", lambda *args, **kwargs: called.append(True))
    invalid = LaunchPlan(
        "workstation-a", "workstation-a", False, (WorkspaceRef("repo", str(tmp_path)),), ("Codex",), "guess-layout"
    )

    with pytest.raises(LaunchPlanError, match="지원하지 않는"):
        plan.execute_launch_plan(
            invalid,
            session="tower-test",
            state_dir=tmp_path / "state",
            agents_cfg={"Codex": "fake-codex"},
        )
    assert called == []


def test_plan_checks_every_workspace_before_spawning_or_recording_recents(tmp_path, monkeypatch):
    valid = tmp_path / "valid"
    valid.mkdir()
    state_dir = tmp_path / "state"
    called = []
    monkeypatch.setattr(plan, "spawn_local", lambda *args, **kwargs: called.append(True))
    launch = LaunchPlan(
        "workstation-a", "workstation-a", False,
        (WorkspaceRef("valid", str(valid)), WorkspaceRef("missing", str(tmp_path / "missing"))),
        ("Codex",), "tiled",
    )

    with pytest.raises(LaunchPlanError, match="작업공간을 확인할 수 없습니다"):
        plan.execute_launch_plan(
            launch,
            session="tower-test",
            state_dir=state_dir,
            agents_cfg={"Codex": "fake-codex"},
        )
    assert called == []
    assert not state_dir.exists()


def test_remote_saved_plan_uses_selected_host_agents_and_layout_with_mocked_ssh(tmp_path, monkeypatch):
    entry = ProjectEntry("remote-repo", "/work/remote-repo", True)
    launches = {}
    monkeypatch.setattr(plan, "validate_remote_path", lambda host, path: PathCheck(True, entry=entry))
    monkeypatch.setattr(plan, "spawn_remote", lambda host, name, targets, cfg, layout, destination=None: launches.update(
        host=host, targets=targets, layout=layout, destination=destination
    ) or [SpawnResult(target, True, "fake") for target in targets])
    launch = LaunchPlan(
        "workstation-b", "workstation-b", True, (WorkspaceRef("remote-repo", "/work/remote-repo"),),
        ("Shell", "Codex"), "even-horizontal"
    )

    results = plan.execute_launch_plan(
        launch,
        session="local-tower",
        state_dir=tmp_path / "state",
        agents_cfg={"Shell": "bash", "Codex": "fake-codex"},
    )

    assert launches["host"] == "workstation-b"
    assert [target.agent_label for target in launches["targets"]] == ["Shell", "Codex"]
    assert launches["layout"] == "even-horizontal"
    assert launches["destination"] is None
    assert len(results) == 2


def test_guided_and_saved_paths_share_the_same_plan_executor():
    assert workspace_browser.execute_launch_plan is workspace_presets.execute_launch_plan
    assert workspace_browser.LaunchPlan is LaunchPlan


def test_saved_run_requires_confirmation_and_reports_the_shared_plan(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    preset = _preset(workspace)
    tower = type("TowerStub", (), {"local_host": "workstation-a", "remote_hosts": [], "session": "tower-test", "overrides": None})()
    calls = []
    monkeypatch.setattr(workspace_presets, "load_config", lambda: {"agents": {"Codex": "fake", "Claude": "fake"}})
    monkeypatch.setattr(workspace_presets, "show_message_screen", lambda *args, **kwargs: None)
    monkeypatch.setattr(workspace_presets, "execute_launch_plan", lambda plan, **kwargs: calls.append(plan) or [])
    monkeypatch.setattr(workspace_presets, "run_list_picker", lambda *args, **kwargs: PickResult(selected_key="cancel"))
    workspace_presets._run_preset(None, tower, tmp_path / "state", preset)
    assert calls == []

    monkeypatch.setattr(workspace_presets, "run_list_picker", lambda *args, **kwargs: PickResult(selected_key="run"))
    workspace_presets._run_preset(None, tower, tmp_path / "state", preset)
    assert calls == [preset.launch_plan()]


def test_saved_run_reports_partial_results_without_pane_ids(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    preset = _preset(workspace)
    tower = type("TowerStub", (), {"local_host": "workstation-a", "remote_hosts": [], "session": "tower-test", "overrides": None})()
    codex = SpawnTarget(str(workspace), "repo", "Codex")
    claude = SpawnTarget(str(workspace), "repo", "Claude")
    results = [
        SpawnResult(codex, True, "시작됨", "%71", "tower-test", "7100"),
        SpawnResult(claude, False, "Agent 명령 실패", "%72", "tower-test", "7200"),
    ]
    screens = []
    monkeypatch.setattr(workspace_presets, "load_config", lambda: {"agents": {"Codex": "fake", "Claude": "fake"}})
    monkeypatch.setattr(workspace_presets, "run_list_picker", lambda *args, **kwargs: PickResult(selected_key="run"))
    monkeypatch.setattr(workspace_presets, "execute_launch_plan", lambda *args, **kwargs: results)
    monkeypatch.setattr(workspace_presets, "show_message_screen", lambda _stdscr, title, lines: screens.append((title, lines)))

    workspace_presets._run_preset(None, tower, tmp_path / "state", preset)

    lines = screens[-1][1]
    assert t("preset.started", name="repo", agent="Codex") in lines
    assert t("preset.not_started", name="repo", detail="Agent 명령 실패") in lines
    assert all("%71" not in line and "%72" not in line for line in lines)


def test_saved_delete_requires_confirmation(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = WorkspacePresetStore(tmp_path / "presets.json")
    first = _preset(workspace, preset_id="first")
    store.save(first)
    monkeypatch.setattr(workspace_presets, "show_message_screen", lambda *args, **kwargs: None)
    monkeypatch.setattr(workspace_presets, "run_list_picker", lambda *args, **kwargs: PickResult(selected_key="cancel"))
    workspace_presets._delete_preset(None, store, first)
    assert store.list() == [first]

    monkeypatch.setattr(workspace_presets, "run_list_picker", lambda *args, **kwargs: PickResult(selected_key="delete"))
    workspace_presets._delete_preset(None, store, first)
    assert store.list() == []


@pytest.mark.parametrize(
    ("layout", "label"),
    [("main-horizontal", "큰 영역 위쪽"), ("main-vertical", "큰 영역 왼쪽")],
)
def test_saved_work_list_and_launch_preview_share_layout_labels(tmp_path, monkeypatch, layout, label):
    preset = _preset(tmp_path, layout=layout)
    list_items = []
    preview_lines = []

    class StoreStub:
        def list(self):
            return [preset]

    def list_picker(_stdscr, _title, items, **kwargs):
        if "searchable" in kwargs:
            list_items.extend(items)
        else:
            preview_lines.extend(kwargs.get("preamble", []))
        return PickResult(selected_key="back" if "searchable" in kwargs else "cancel")

    monkeypatch.setattr(workspace_presets, "WorkspacePresetStore", StoreStub)
    monkeypatch.setattr(workspace_presets, "run_list_picker", list_picker)
    workspace_presets.open_workspace_presets(None, None, tmp_path)

    tower = type("TowerStub", (), {"local_host": "workstation-a", "remote_hosts": [], "session": "tower-test", "overrides": None})()
    workspace_presets._run_preset(None, tower, tmp_path, preset)

    listed_label = next(text for key, text in list_items if key == preset.preset_id)
    assert label in listed_label
    assert t("preset.layout_line", layout=label) in preview_lines
