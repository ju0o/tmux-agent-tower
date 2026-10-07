import pytest

from tmux_agent_tower.launcher.spawn import SpawnResult, SpawnTarget
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.ui import structure_menu, workspace_browser
from tmux_agent_tower.ui.widgets import PickResult


def test_create_task_asks_plain_flow_and_keeps_task_name_separate(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher.browse import ProjectEntry

    overrides = OverrideStore(tmp_path / "overrides.json")
    tower = type("TowerStub", (), {})()
    tower.session = "main"
    tower.selected = 0
    tower.visible_rows = [{"kind": "pane", "window_id": "@current"}]
    tower.rows = []
    tower.overrides = overrides
    tower.load = lambda: None
    pick = workspace_browser.WorkspacePick(
        "workstation-a", "내 컴퓨터", False, ProjectEntry("SamplePortal", "/work/SamplePortal", True)
    )
    seen = {}
    flow = []

    monkeypatch.setattr(workspace_browser, "prompt_text", lambda _s, title: seen.setdefault("name_prompt", title) and "로그인 버그 수정")
    monkeypatch.setattr(workspace_browser, "current_project_entry", lambda: None)
    monkeypatch.setattr(workspace_browser, "pick_workspaces", lambda *a, **k: [pick])
    monkeypatch.setattr(workspace_browser, "_pick_agent", lambda *a, **k: flow.append("agent") or "Codex")
    monkeypatch.setattr(workspace_browser, "_pick_role", lambda *a, **k: flow.append("role") or "builder")
    monkeypatch.setattr(
        workspace_browser,
        "_pick_placement",
        lambda *a, **k: pytest.fail("quick task uses safe default placement"),
    )
    monkeypatch.setattr(workspace_browser, "_pick_layout", lambda *a, **k: pytest.fail("quick task uses auto layout"))
    monkeypatch.setattr(
        workspace_browser,
        "run_list_picker",
        lambda *a, **k: PickResult(selected_key="create"),
    )

    def launch(plan, **kwargs):
        seen["plan"] = plan
        seen.update(kwargs)
        target = workspace_browser.SpawnTarget("/work/SamplePortal", "SamplePortal", "Codex")
        return [SpawnResult(target, True, "시작됨", "%51", "main", "5151")]

    monkeypatch.setattr(workspace_browser, "execute_launch_plan", launch)
    opened = []
    from tmux_agent_tower.ui import control_view
    monkeypatch.setattr(control_view, "open_control_view", lambda _s, _t, key: opened.append(key))

    workspace_browser.run_workspace_create(
        None, tower, tmp_path, multi=False, open_tree=True, task_flow=True
    )

    assert "name_prompt" in seen
    assert seen["plan"].host_key == "workstation-a"
    assert [(workspace.name, workspace.path) for workspace in seen["plan"].workspaces] == [
        ("SamplePortal", "/work/SamplePortal")
    ]
    assert seen["plan"].agents == ("Codex",)
    assert seen["plan"].placement == "new"
    assert seen["plan"].layout == "tiled"
    assert flow == ["agent", "role"]
    assert opened == ["%51"]
    assert overrides.get_task_name("%51", "main", "5151") == "로그인 버그 수정"
    assert overrides.get_role("%51", "main", "5151") == "builder"
    assert overrides.get_project("%51", "main", "5151") is None


def test_create_hub_saved_item_opens_saved_work_manager(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_ui, worksets

    tower = object()
    opened = []
    root_picks = iter(("saved",))
    collection_picks = iter(("saved_work",))
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *a, **k: PickResult(selected_key=next(root_picks)))
    monkeypatch.setattr(worksets, "run_list_picker", lambda *a, **k: PickResult(selected_key=next(collection_picks)))
    monkeypatch.setattr(tower_ui, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        worksets,
        "open_saved_work",
        lambda stdscr, selected_tower, state_dir: opened.append((stdscr, selected_tower, state_dir)),
    )

    structure_menu.open_create_hub(None, tower)

    assert opened == [(None, tower, tmp_path)]


def test_saved_work_entry_routes_saved_work_and_templates_through_one_hub(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import worksets

    picks = iter(("saved_work", "template"))
    opened = []
    monkeypatch.setattr(
        worksets, "run_list_picker",
        lambda _screen, _title, items, **_kwargs: PickResult(selected_key=next(picks)),
    )
    monkeypatch.setattr(worksets, "open_saved_work", lambda *args: opened.append(("saved", args)))
    monkeypatch.setattr(worksets, "open_work_templates", lambda *args: opened.append(("template", args)))

    worksets.open_saved_collection("screen", "tower", tmp_path)
    worksets.open_saved_collection("screen", "tower", tmp_path)

    assert [kind for kind, _args in opened] == ["saved", "template"]
    assert all(args == ("screen", "tower", tmp_path) for _kind, args in opened)


def test_create_hub_keeps_advanced_entries_under_more(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        structure_menu, "run_list_picker",
        lambda _screen, title, items, **_kwargs: captured.update(title=title, items=items)
        or PickResult(selected_key="cancel"),
    )

    structure_menu.open_create_hub(None, object())

    keys = [key for key, _label in captured["items"]]
    assert keys == ["task", "group", "saved", "template", "more", "cancel"]


def test_task_menu_separates_result_and_screen_copy(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.ui import control_view

    row = {"kind": "pane", "key": "%9", "pane_id": "%9", "target_id": "target-9", "project": "Tower"}
    tower = SimpleNamespace(work_groups=SimpleNamespace(membership=lambda: set()))
    picks = iter(("cancel", "more", "screen_copy"))
    menus = []

    def pick(_screen, _title, items, **_kwargs):
        menus.append(items)
        return PickResult(selected_key=next(picks))

    copied = []
    monkeypatch.setattr(structure_menu, "run_list_picker", pick)
    monkeypatch.setattr(control_view, "_copy_screen", lambda *_args: copied.append(True) or "화면을 복사했습니다")
    monkeypatch.setattr(structure_menu, "show_message_screen", lambda *_args: None)

    # First inspect the ordinary task actions, then route More → current screen copy.
    structure_menu.open_pane_menu(None, tower, row)
    structure_menu.open_pane_menu(None, tower, row)

    assert [key for key, _label in menus[0]] == [
        "control", "result", "copy", "edit", "move", "layout", "more", "cancel"
    ]
    assert ("screen_copy", structure_menu.t("nav.copy_screen")) in menus[2]
    assert copied == [True]


def test_create_remote_task_binds_name_to_exact_namespaced_remote_identity(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher.browse import ProjectEntry

    overrides = OverrideStore(tmp_path / "overrides.json")
    tower = type("TowerStub", (), {})()
    tower.session = "main"
    tower.selected = 0
    tower.visible_rows = []
    tower.rows = []
    tower.overrides = overrides
    overrides.set_project("workstation-b:%51", "오래된 프로젝트", "old-session", "old-pid")
    overrides.set_agent("workstation-b:%51", "오래된 Agent", "old-session", "old-pid")
    pick = workspace_browser.WorkspacePick(
        "workstation-b", "workstation-b", True, ProjectEntry("SamplePortal", "/work/SamplePortal", True)
    )
    seen = {}
    monkeypatch.setattr(workspace_browser, "prompt_text", lambda *_: "로그인 버그 수정")
    monkeypatch.setattr(workspace_browser, "pick_workspaces", lambda *a, **k: [pick])
    monkeypatch.setattr(workspace_browser, "_pick_agent", lambda *a, **k: "Codex")
    monkeypatch.setattr(workspace_browser, "_pick_role", lambda *a, **k: "e2e")
    monkeypatch.setattr(workspace_browser, "_pick_placement", lambda *a, **k: ("new", "새 작업 묶음"))
    monkeypatch.setattr(workspace_browser, "_pick_layout", lambda *a, **k: ("auto", "자동"))
    monkeypatch.setattr(workspace_browser, "run_list_picker", lambda *a, **k: PickResult(selected_key="create"))
    def execute(plan, **kwargs):
        seen["plan"] = plan
        return [SpawnResult(
            SpawnTarget("/work/SamplePortal", "SamplePortal", "Codex"), True, "시작됨", "%51", "remote-session", "5151"
        )]
    monkeypatch.setattr(workspace_browser, "execute_launch_plan", execute)
    monkeypatch.setattr(workspace_browser, "show_message_screen", lambda *a, **k: None)

    workspace_browser.run_workspace_create(None, tower, tmp_path, multi=False, task_flow=True)

    assert seen["plan"].host_key == "workstation-b"
    assert seen["plan"].agents == ("Codex",)
    assert seen["plan"].placement == "new"
    assert overrides.get_task_name("workstation-b:%51", "remote-session", "5151") == "로그인 버그 수정"
    assert overrides.get_role("workstation-b:%51", "remote-session", "5151") == "e2e"
    assert overrides.get_task_name("%51", "remote-session", "5151") is None
    assert overrides.get_task_name("workstation-b:%51", "remote-session", "5152") is None
    assert overrides.get_task_name("workstation-b:%51", "other-session", "5151") is None
    assert overrides.get_project("workstation-b:%51", "remote-session", "5151") is None
    assert overrides.get_agent("workstation-b:%51", "remote-session", "5151") is None


def test_same_agent_can_keep_different_roles_on_two_tasks(tmp_path):
    overrides = OverrideStore(tmp_path / "overrides.json")
    overrides.set_task_name("%61", "기능 구현", "main", "610")
    overrides.set_task_name("%62", "구현 검수", "main", "620")
    overrides.set_role("%61", "builder", "main", "610")
    overrides.set_role("%62", "reviewer", "main", "620")

    # Agent identity remains in the live row, independently of role metadata.
    rows = [
        {"key": "%61", "session": "main", "pane_pid": "610", "agent": "Codex"},
        {"key": "%62", "session": "main", "pane_pid": "620", "agent": "Codex"},
    ]
    assert [(row["agent"], overrides.get_role(row["key"], row["session"], row["pane_pid"])) for row in rows] == [
        ("Codex", "builder"), ("Codex", "reviewer")
    ]


def test_change_role_is_a_simple_metadata_action_and_preserves_task_identity(tmp_path, monkeypatch):
    overrides = OverrideStore(tmp_path / "overrides.json")
    row = {
        "key": "workstation-b:%71", "pane_id": "%71", "remote": True,
        "session": "peer", "pane_pid": "710", "task_name": "접속 오류 검수",
        "project": "SamplePortal", "agent": "Codex", "role": "planner",
    }
    tower = type("TowerStub", (), {"load": lambda self: None})()
    tower.rows = [row]
    tower.overrides = overrides
    overrides.set_project(row["key"], "SamplePortal", row["session"], row["pane_pid"])
    overrides.set_task_name(row["key"], row["task_name"], row["session"], row["pane_pid"])
    overrides.set_agent(row["key"], row["agent"], row["session"], row["pane_pid"])
    seen = {}
    monkeypatch.setattr(
        structure_menu,
        "run_list_picker",
        lambda _s, title, items, **kwargs: seen.update(title=title, items=items, preamble=kwargs["preamble"])
        or PickResult(selected_key="reviewer"),
    )

    structure_menu.change_role(None, tower, row)

    assert seen["title"] == structure_menu.t("role.change")
    assert "Pane" not in " ".join(label for _key, label in seen["items"])
    assert overrides.get_role("workstation-b:%71", "peer", "710") == "reviewer"
    assert overrides.get_project("workstation-b:%71", "peer", "710") == "SamplePortal"
    assert overrides.get_task_name("workstation-b:%71", "peer", "710") == "접속 오류 검수"
    assert overrides.get_agent("workstation-b:%71", "peer", "710") == "Codex"
    assert (row["task_name"], row["project"], row["agent"]) == ("접속 오류 검수", "SamplePortal", "Codex")


@pytest.mark.parametrize("selected_role", ["__unassigned__", "reviewer"])
def test_change_role_does_not_touch_reused_pane_after_picker(tmp_path, monkeypatch, selected_role):
    from tmux_agent_tower.i18n.ko import STRINGS

    overrides = OverrideStore(tmp_path / "overrides.json")
    original = {
        "key": "%72", "pane_id": "%72", "session": "main", "pane_pid": "720",
        "task_name": "기존 작업", "role": "builder",
    }
    replacement = {**original, "pane_pid": "721", "task_name": "새 작업", "role": None}
    tower = type("TowerStub", (), {"load": lambda self: None})()
    tower.rows = [original]
    tower.overrides = overrides
    overrides.set_role("%72", "builder", "main", "720")
    messages = []

    def picker(*_args, **_kwargs):
        tower.rows = [replacement]
        return PickResult(selected_key=selected_role)

    monkeypatch.setattr(structure_menu, "run_list_picker", picker)
    monkeypatch.setattr(structure_menu, "show_message_screen", lambda _s, title, lines: messages.append((title, lines)))

    structure_menu.change_role(None, tower, original)

    assert messages == [(STRINGS["role.stale"], [])]
    assert overrides.get_role("%72", "main", "720") == "builder"
    assert overrides.get_role("%72", "main", "721") is None


def test_remote_pane_menu_keeps_role_change_without_physical_actions(monkeypatch):
    row = {"key": "workstation-b:%71", "pane_id": "%71", "remote": True, "project": "SamplePortal", "agent": "Codex"}
    seen = {}
    monkeypatch.setattr(
        structure_menu,
        "run_list_picker",
        lambda _s, _title, items, **kwargs: seen.update(items=items)
        or PickResult(selected_key="cancel"),
    )
    structure_menu.open_pane_menu(None, object(), row)
    keys = [key for key, _label in seen["items"]]
    assert "role" in keys
    assert "physical" not in keys
    assert "close" not in keys


def test_task_placement_shows_only_plain_korean_choices(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        workspace_browser,
        "run_list_picker",
        lambda _s, title, items, **kwargs: captured.update(title=title, items=items) or PickResult(selected_key="current"),
    )

    assert workspace_browser._pick_placement(None, False, [], "", task_flow=True) == (
        "current", workspace_browser.t("struct.place_beside")
    )
    assert [label for _key, label in captured["items"]] == [
        workspace_browser.t("struct.place_group"),
        workspace_browser.t("struct.place_beside"),
        workspace_browser.t("menu.cancel"),
    ]
    assert all("Window" not in label and "Pane" not in label for _key, label in captured["items"])


def test_role_picker_has_korean_defaults_and_an_unassigned_choice(monkeypatch):
    from tmux_agent_tower.i18n.ko import STRINGS

    captured = {}
    monkeypatch.setattr(
        workspace_browser,
        "run_list_picker",
        lambda _s, title, items, **kwargs: captured.update(title=title, items=items)
        or PickResult(selected_key="__unassigned__"),
    )
    assert workspace_browser._pick_role(None, ["Codex"]) == ""
    assert captured["title"] == STRINGS["wizard.pick_role"]
    assert dict(captured["items"]) == {
        "orchestrator": "조율", "planner": "계획", "builder": "구현",
        "reviewer": "검수", "qa": "확인", "dogfood": "실사용",
        "e2e": "전체 흐름", "__unassigned__": "역할 없음", "cancel": STRINGS["menu.cancel"],
    }


def test_task_details_show_role_separately_from_project_agent_and_status():
    from types import SimpleNamespace
    from tmux_agent_tower.ui.tower import _build_detail_fields

    tower = SimpleNamespace(config={"show_status_duration": False, "show_activity": False})
    fields = _build_detail_fields(tower, {
        "project": "SamplePortal", "task_name": "로그인 검수", "agent": "Codex",
        "role": "reviewer", "status": "IDLE", "attention": "none", "result_state": "none",
    })
    values = dict(fields)
    assert values["작업 이름"] == "로그인 검수"
    assert values["프로젝트"] == "SamplePortal"
    assert values["에이전트"] == "Codex"
    assert values["역할"] == "검수"
    assert values["상태"]


def test_host_picker_uses_discovered_host_labels(monkeypatch):
    tower = type("TowerStub", (), {})()
    tower.local_host = "workstation-a"
    tower.remote_hosts = [{"alias": "workstation-b", "name": "workstation-b"}]
    captured = {}
    monkeypatch.setattr(
        workspace_browser,
        "run_list_picker",
        lambda _s, title, items, **kwargs: captured.update(title=title, items=items)
        or PickResult(selected_key="workstation-b"),
    )

    assert workspace_browser._pick_host(None, tower) == ("workstation-b", "workstation-b", True)
    assert captured["items"] == [("workstation-a", "workstation-a"), ("workstation-b", "workstation-b")]


def test_move_task_changes_group_membership_without_moving_pane(monkeypatch, tmp_path):
    from tmux_agent_tower.state.work_groups import WorkGroupStore

    tower = type("TowerStub", (), {})()
    tower.session = "main"
    tower.own_pane_id = "%tower"
    tower.load = lambda: None
    tower.rows = [
        {"target_id": "target-9", "pane_id": "%9", "window_id": "@1", "task_name": "현재 작업", "project": "SamplePortal"},
        {"target_id": "target-20", "pane_id": "%20", "window_id": "@2", "task_name": "검토 작업", "project": "Agent-Relay"},
    ]
    tower.work_groups = WorkGroupStore(tmp_path / "groups.json")
    source = tower.work_groups.create("SamplePortal", ["target-9"])
    dest = tower.work_groups.create("Agent-Relay", ["target-20"])
    seen = {}

    def picker(_s, title, items, **kwargs):
        seen.setdefault("pickers", []).append((title, items, kwargs))
        return PickResult(selected_key=dest["group_id"])

    monkeypatch.setattr(structure_menu, "run_list_picker", picker)
    monkeypatch.setattr(structure_menu.structure, "move_pane", lambda *_a: pytest.fail("logical move touched tmux"))

    structure_menu.move_work(None, tower, tower.rows[0])

    assert seen["pickers"][0][0] == structure_menu.t("move.pick_group")
    assert seen["pickers"][0][1] == [(dest["group_id"], "Agent-Relay"), ("__ungroup__", structure_menu.t("move.ungroup")), ("__new__", structure_menu.t("move.new_group"))]
    assert tower.work_groups.membership() == {"target-9": dest["group_id"], "target-20": dest["group_id"]}
    assert [(row["pane_id"], row["window_id"]) for row in tower.rows] == [("%9", "@1"), ("%20", "@2")]


def test_physical_terminal_move_refuses_remote_execution(monkeypatch):
    from types import SimpleNamespace

    remote = {"target_id": "remote-target", "key": "server:%9", "pane_id": "%9", "remote": True}
    tower = SimpleNamespace(load=lambda: None, rows=[remote], session="local")
    monkeypatch.setattr(structure_menu, "show_message_screen", lambda *_a, **_k: None)
    monkeypatch.setattr(structure_menu, "run_list_picker", lambda *_a, **_k: pytest.fail(
        "remote panes must be rejected before destination selection"
    ))

    structure_menu.move_pane_physical(None, tower, remote)


def test_create_hub_offers_plain_new_task_label(monkeypatch):
    from tmux_agent_tower.i18n import en, ko

    captured = {}
    monkeypatch.setattr(
        structure_menu,
        "run_list_picker",
        lambda _s, title, items, **_kwargs: captured.update(title=title, items=items)
        or PickResult(selected_key="cancel"),
    )

    structure_menu.open_create_hub(None, object())

    labels = dict(captured["items"])
    assert labels["task"] == structure_menu.t("struct.create_task")
    assert ko.STRINGS["struct.create_task"] == "작업 하나 시작"
    for catalog in (ko.STRINGS, en.STRINGS):
        for key in (
            "struct.create_task",
            "struct.act_move",
            "struct.place_window",
            "struct.place_current",
            "struct.place_pick",
            "struct.move_title",
            "struct.move_confirm",
            "browser.layout",
            "browser.layout_main_h",
            "browser.layout_main_v",
        ):
            assert not any(
                term in catalog[key].lower()
                for term in ("window", "pane", "break-pane", "move-pane", "pane_id", "window_id")
            )
    assert not any(
        term in label.lower()
        for label in labels.values()
        for term in ("window", "pane", "break-pane", "move-pane", "pane_id", "window_id")
    )


def test_existing_work_group_picker_hides_tmux_window_ids(monkeypatch):
    from tmux_agent_tower.i18n import en, ko

    tower = type("TowerStub", (), {"session": "main"})()
    captured = {}
    monkeypatch.setattr(
        workspace_browser.structure,
        "list_windows",
        lambda _session: [
            {"window_id": "@17", "window_index": "0", "window_name": "Alpha"},
            {"window_id": "@28", "window_index": "1", "window_name": "Beta"},
        ],
    )
    monkeypatch.setattr(
        workspace_browser,
        "run_list_picker",
        lambda _s, _title, items, **_kwargs: captured.update(items=items)
        or PickResult(selected_key="cancel"),
    )

    workspace_browser._pick_window(None, tower)

    assert captured["items"][:2] == [("@17", "1. Alpha"), ("@28", "2. Beta")]
    assert all("@" not in label for _key, label in captured["items"])
    assert all(
        not any(term in label.lower() for term in ("window", "pane", "window_id", "pane_id"))
        for _key, label in captured["items"]
    )
    for catalog in (ko.STRINGS, en.STRINGS):
        assert not any(
            term in catalog[key].lower()
            for key in ("struct.place_pick", "browser.layout", "browser.layout_main_h", "browser.layout_main_v")
            for term in ("window", "pane", "window_id", "pane_id")
        )
