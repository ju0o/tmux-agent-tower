import json
import stat

import pytest

from tmux_agent_tower.state.worksets import WorkMember, WorksetError, WorksetStore, new_workset, phone_summary


def _members(path, *, kind="saved_work"):
    return [
        WorkMember("", "orchestrator", "Claude", project_name="Project A" if kind == "saved_work" else "", project_path=str(path) if kind == "saved_work" else ""),
        WorkMember("", "builder", "Codex", project_name="Project A" if kind == "saved_work" else "", project_path=str(path) if kind == "saved_work" else ""),
        WorkMember("", "qa", "OpenCode", project_name="Project A" if kind == "saved_work" else "", project_path=str(path) if kind == "saved_work" else ""),
    ]


def test_saved_work_and_template_round_trip_without_tmux_ids(tmp_path):
    project = tmp_path / "project-a"
    project.mkdir()
    store = WorksetStore(tmp_path / "worksets.json")
    saved = new_workset(kind="saved_work", name="Project A · V2", members=_members(project))
    template = new_workset(kind="template", name="기능 고도화 팀", members=_members(project, kind="template"))
    store.save(saved)
    store.save(template)

    reopened = WorksetStore(store.path)
    assert reopened.list("saved_work") == [saved]
    assert reopened.list("template") == [template]
    encoded_template = json.loads(store.path.read_text(encoding="utf-8"))["worksets"][1]
    assert not any("pane" in key or "%" in str(value) for row in encoded_template["members"] for key, value in row.items())
    assert all(not row["project_path"] and not row["project_name"] for row in encoded_template["members"])
    assert [row.layout_slot for row in template.members] == ["main", "side-1", "side-2"]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_workset_order_and_duplicate_names_are_identity_independent(tmp_path):
    store = WorksetStore(tmp_path / "worksets.json")
    first = new_workset(kind="template", name="same", members=_members(tmp_path, kind="template"))
    second = new_workset(kind="template", name="same", members=_members(tmp_path, kind="template"))
    store.save(first)
    store.save(second)
    assert [item.workset_id for item in store.list("template")] == [first.workset_id, second.workset_id]
    assert first.member_order == tuple(row.member_id for row in first.members)


def test_corrupt_and_future_documents_are_not_overwritten(tmp_path):
    path = tmp_path / "worksets.json"
    for raw in ('{bad', json.dumps({"version": 2, "worksets": []})):
        path.write_text(raw, encoding="utf-8")
        store = WorksetStore(path)
        with pytest.raises(WorksetError):
            store.save(new_workset(kind="template", name="x", members=_members(tmp_path, kind="template")))
        assert path.read_text(encoding="utf-8") == raw


def test_template_rejects_project_and_concrete_execution_values(tmp_path):
    bad = new_workset(kind="template", name="x", members=_members(tmp_path, kind="template"))
    member = bad.members[0]
    from dataclasses import replace

    invalid = replace(bad, members=(replace(member, project_path="/private/project"),) + bad.members[1:])
    with pytest.raises(WorksetError):
        WorksetStore(tmp_path / "worksets.json").save(invalid)


def test_current_group_saves_as_saved_work_or_project_neutral_template(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tmux_agent_tower.ui import worksets as workset_ui

    project = tmp_path / "ExampleProject"
    project.mkdir()
    rows = [
        {"target_id": "stable-a", "key": "%17", "session": "s", "pane_pid": "170",
         "role": "builder", "agent": "Codex", "display_name": "ExampleProject · 구현", "name_origin": "user",
         "project": "ExampleProject", "project_path": str(project), "path": str(project)},
        {"target_id": "stable-b", "key": "%18", "session": "s", "pane_pid": "180",
         "role": "qa", "agent": "Claude", "display_name": "ExampleProject · 확인", "name_origin": "auto",
         "project": "ExampleProject", "project_path": str(project), "path": str(project)},
    ]
    tower = SimpleNamespace(
        rows=rows,
        load=lambda: None,
        overrides=SimpleNamespace(get_execution_host=lambda *_: None),
    )
    group = {"display_name": "ExampleProject · V2", "member_order": ["stable-a", "stable-b"]}
    store = WorksetStore(tmp_path / "worksets.json")
    monkeypatch.setattr(workset_ui, "WorksetStore", lambda: store)
    monkeypatch.setattr(workset_ui, "run_list_picker", lambda *_a, **_k: SimpleNamespace(selected_key="saved_work"))
    monkeypatch.setattr(workset_ui, "prompt_text", lambda *_a, **_k: "ExampleProject · V2")
    monkeypatch.setattr(workset_ui, "show_message_screen", lambda *_a, **_k: None)
    workset_ui.save_work_group(None, tower, group)
    saved = store.list("saved_work")[0]
    assert [row.role for row in saved.members] == ["builder", "qa"]
    assert [row.agent for row in saved.members] == ["Codex", "Claude"]
    assert saved.members[0].display_name == "ExampleProject · 구현"
    assert all(row.project_path == str(project) for row in saved.members)

    monkeypatch.setattr(workset_ui, "run_list_picker", lambda *_a, **_k: SimpleNamespace(selected_key="template"))
    workset_ui.save_work_group(None, tower, group)
    template = store.list("template")[0]
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert [row.role for row in template.members] == ["builder", "qa"]
    assert [row.agent for row in template.members] == ["Codex", "Claude"]
    assert all(not row.project_path and not row.project_name and not row.display_name for row in template.members)
    encoded_template = next(row for row in raw["worksets"] if row["kind"] == "template")
    assert str(project) not in json.dumps(encoded_template)
    assert "%17" not in json.dumps(encoded_template) and "%18" not in json.dumps(encoded_template)


def test_phone_workset_summary_never_contains_paths_or_execution_hosts(tmp_path):
    project = tmp_path / "private-project"
    project.mkdir()
    saved = new_workset(kind="saved_work", name="V2", members=[
        WorkMember("", "builder", "Codex", display_name="구현", project_name="Private Project",
                   project_path=str(project), execution_target="personal-host"),
    ])
    summary = phone_summary([saved])
    assert summary[0]["project"] == "Private Project"
    assert summary[0]["members"][0]["role_label"] == "구현"
    assert str(project) not in str(summary)
    assert "personal-host" not in str(summary)
