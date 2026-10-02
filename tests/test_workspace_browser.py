"""Workspace browser: paths stay visible, browse is lazy, hosts stay separate."""

import subprocess

from tmux_agent_tower.i18n import en, ko
from tmux_agent_tower.launcher.browse import (
    DirectoryCache,
    classify_remote_paths,
    collect_local_catalog,
    collect_remote_catalog,
    filter_entries,
    format_entry_lines,
    kind_label,
    list_local_children,
    list_remote_children,
    local_start_roots,
    parent_path,
    remote_catalog_script,
    remote_classify_script,
    remote_list_script,
    remote_roots_script,
    remote_start_roots,
    remote_validate_script,
    script_is_read_only,
    validate_local_path,
    validate_remote_path,
)
from tmux_agent_tower.launcher.discovery import find_git_projects, load_recent
from tmux_agent_tower.launcher.spawn import SpawnResult
from remote_fixtures import (
    REMOTE_HOME,
    REMOTE_MISSING,
    REMOTE_NOTES,
    REMOTE_OTHER,
    REMOTE_PROJECTS,
    REMOTE_REPO,
)
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.ui.workspace_browser import (
    WorkspacePick,
    host_choices,
    launch_workspaces,
    placement_choices,
)
from test_launcher_spawn import FakeTmux


def _git(path):
    (path / ".git").mkdir()


def test_browser_catalogs_match():
    assert {key for key in ko.STRINGS if key.startswith("browser.")} == {
        key for key in en.STRINGS if key.startswith("browser.")
    }
    assert ko.STRINGS["zero.task"] == "새 작업 시작"
    assert ko.STRINGS["zero.pane"] == "빈 Pane"


def test_entry_shows_full_path_on_wide_and_narrow():
    lines = format_entry_lines("JuPortal", "/mnt/f/JuPortal", False, 120)
    assert "/mnt/f/JuPortal" in " ".join(lines)
    assert "Folder" in lines[0]
    narrow = format_entry_lines("JuPortal", "/mnt/f/JuPortal", False, 40)
    assert narrow[0].startswith("JuPortal")
    assert narrow[1] == "/mnt/f/JuPortal"
    assert kind_label(True) == "Git"


def test_direct_path_is_canonical_and_invalid_path_is_not_rewritten(tmp_path):
    folder = tmp_path / "JuPortal"
    folder.mkdir()
    link = tmp_path / "link"
    link.symlink_to(folder, target_is_directory=True)

    found = validate_local_path(str(link))
    assert found.ok
    assert found.entry.path == str(folder.resolve())
    assert found.entry.is_git is False

    missing = str(tmp_path / "no-such-project")
    sibling = tmp_path / "actual"
    sibling.mkdir()
    bad = validate_local_path(missing)
    assert bad.ok is False
    assert bad.entry is None
    assert bad.error.path == missing
    assert bad.error.code == "not_found"
    assert sibling.name not in bad.error.path

    file_path = tmp_path / "notes.txt"
    file_path.write_text("x", encoding="utf-8")
    not_dir = validate_local_path(str(file_path))
    assert not_dir.error.code == "not_dir"
    assert not_dir.error.path == str(file_path)


def test_folder_and_git_selection_use_basename(tmp_path):
    folder = tmp_path / "Plain"
    folder.mkdir()
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _git(repo)

    plain = validate_local_path(str(folder))
    git = validate_local_path(str(repo))
    assert plain.entry.name == "Plain" and plain.entry.is_git is False
    assert git.entry.name == "my-repo" and git.entry.is_git is True
    assert git.entry.path == str(repo.resolve())


def test_local_tree_lists_only_direct_children_and_parent(tmp_path):
    root = tmp_path / "mnt"
    projects = root / "Projects"
    experiments = projects / "Experiments"
    repo = experiments / "tower"
    repo.mkdir(parents=True)
    _git(repo)
    (root / "JuPortal").mkdir()

    page = list_local_children(str(root))
    assert page.ok
    assert {child.name for child in page.children} == {"JuPortal", "Projects"}
    assert "tower" not in {child.name for child in page.children}

    opened = list_local_children(str(projects))
    assert [child.name for child in opened.children] == ["Experiments"]
    assert parent_path(opened.path) == str(root.resolve())
    top = list_local_children(parent_path(opened.path))
    assert "Experiments" not in {child.name for child in top.children}


def test_configured_roots_skip_missing_and_include_home_and_drives(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "Projects"
    root.mkdir()
    mnt = tmp_path / "mnt"
    drive = mnt / "f"
    drive.mkdir(parents=True)
    (mnt / "file").write_text("x", encoding="utf-8")

    roots = local_start_roots(
        [str(root), str(tmp_path / "missing")],
        [],
        home=str(home),
        mnt=str(mnt),
    )
    assert roots[0] == str(root.resolve())
    assert str(home.resolve()) in roots
    assert str(drive.resolve()) in roots
    assert str(tmp_path / "missing") not in roots


def test_search_finds_past_the_first_screen_and_a_plain_folder(tmp_path):
    for index in range(30):
        repo = tmp_path / f"repo-{index:02d}"
        repo.mkdir()
        _git(repo)
    notes = tmp_path / "field-notes"
    notes.mkdir()

    catalog = collect_local_catalog([str(tmp_path)])
    assert len(catalog) > 20
    found = filter_entries(catalog, "repo-29")
    assert [entry.path for entry in found] == [str((tmp_path / "repo-29").resolve())]
    assert found[0].is_git is True
    folder = filter_entries(catalog, "field-notes")
    assert folder[0].is_git is False
    assert folder[0].path == str(notes.resolve())
    assert find_git_projects([str(tmp_path / "repo-00")])[0].name == "repo-00"


def test_browse_scripts_do_not_write_or_start_tmux():
    scripts = [
        remote_list_script(REMOTE_PROJECTS),
        remote_validate_script(REMOTE_REPO),
        remote_roots_script(),
        remote_catalog_script(),
        remote_classify_script([REMOTE_REPO]),
    ]
    for script in scripts:
        assert script_is_read_only(script)
        assert "pwd -P" in script or "__HOME__" in script or "dirname" in script


def test_remote_directory_browse_and_git_detection(monkeypatch):
    calls = []

    def runner(host, script, timeout):
        calls.append(script)
        assert script_is_read_only(script)
        stdout = "\n".join([
            "__STATUS__ ok",
            f"__PATH__ {REMOTE_PROJECTS}",
            "__GIT__ 0",
            f"sample-repo\t{REMOTE_REPO}\t1",
            f"notes\t{REMOTE_PROJECTS}/notes\t0",
        ])
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    page = list_remote_children("asus", REMOTE_PROJECTS, runner=runner)
    assert page.ok
    assert page.path == REMOTE_PROJECTS
    assert [(child.name, child.is_git) for child in page.children] == [("notes", False), ("sample-repo", True)]
    assert "maxdepth 1" in calls[0]

    def validate(host, script, timeout):
        assert "tmux" not in script
        stdout = "\n".join([
            "__STATUS__ ok",
            f"__PATH__ {REMOTE_REPO}",
            "__GIT__ 1",
            "__NAME__ sample-repo",
        ])
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    checked = validate_remote_path("asus", REMOTE_REPO, runner=validate)
    assert checked.ok and checked.entry.is_git and checked.entry.name == "sample-repo"


def test_remote_invalid_path_keeps_the_typed_path():
    def runner(host, script, timeout):
        stdout = f"__STATUS__ missing\n__PATH__ {REMOTE_OTHER}\n"
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    typed = REMOTE_MISSING
    checked = validate_remote_path("asus", typed, runner=runner)
    assert checked.ok is False
    assert checked.error.code == "not_found"
    assert checked.error.path == typed


def test_remote_timeout_is_unreachable_and_cached_listing_is_not_refetched():
    def runner(host, script, timeout):
        return "timeout"

    checked = validate_remote_path("asus", REMOTE_HOME, runner=runner)
    assert checked.error.code == "unreachable"
    assert checked.error.path == REMOTE_HOME

    calls = {"n": 0}

    def once(host, script, timeout):
        calls["n"] += 1
        stdout = "\n".join(["__STATUS__ ok", f"__PATH__ {REMOTE_HOME}", "__GIT__ 0"])
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    cache = DirectoryCache()
    first = list_remote_children("asus", REMOTE_HOME, cache=cache, runner=once)
    second = list_remote_children("asus", REMOTE_HOME, cache=cache, runner=once)
    assert first.ok and second.path == first.path
    assert calls["n"] == 1


def test_remote_roots_and_recent_stay_on_that_host():
    def runner(host, script, timeout):
        stdout = "\n".join([
            f"__HOME__ {REMOTE_HOME}",
            f"__ROOT__ {REMOTE_PROJECTS}",
        ])
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    roots, error = remote_start_roots("asus", [REMOTE_REPO], runner=runner)
    assert error is None
    assert roots[0] == REMOTE_PROJECTS
    assert REMOTE_REPO in roots
    assert "/mnt/f/JuPortal" not in roots


def test_recent_lists_are_isolated_and_remote_classify_is_one_call(tmp_path):
    from tmux_agent_tower.launcher.discovery import record_recent

    record_recent(tmp_path, "MAINPC", "/mnt/f/JuPortal")
    record_recent(tmp_path, "ASUS", REMOTE_REPO)
    assert load_recent(tmp_path, "MAINPC") == ["/mnt/f/JuPortal"]
    assert load_recent(tmp_path, "ASUS") == [REMOTE_REPO]

    def runner(host, script, timeout):
        assert script_is_read_only(script)
        stdout = f"sample-repo\t{REMOTE_REPO}\t1\n"
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    entries, error = classify_remote_paths("asus", load_recent(tmp_path, "ASUS"), runner=runner)
    assert error is None
    assert entries[0].is_git is True
    assert entries[0].path == REMOTE_REPO


def test_host_picker_keeps_a_single_host_and_remote_placement_is_a_new_window():
    only = host_choices("MAINPC", [])
    assert only == [("MAINPC", "MAINPC", False)]
    both = host_choices("MAINPC", [{"alias": "asus", "name": "ASUS"}])
    assert [item[0] for item in both] == ["MAINPC", "asus"]
    assert placement_choices(True) == ["new"]
    assert placement_choices(False) == ["new", "current", "pick"]


def test_create_uses_selected_path_host_and_agent_and_drops_stale_override(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher import spawn

    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))
    project = tmp_path / "tower-repo"
    project.mkdir()
    _git(project)
    entry = validate_local_path(str(project)).entry
    overrides = OverrideStore(tmp_path / "overrides.json")
    overrides.set_agent("%1", "CommandCode", "0", "111")
    overrides.set_project("%1", "JuPortal", "0", "111")
    pick = WorkspacePick("MAINPC", "MAINPC", False, entry)

    results = launch_workspaces(
        session="0",
        state_dir=tmp_path,
        picks=[pick],
        agent="Cursor",
        placement="new",
        layout_choice="auto",
        overrides=overrides,
        agents_cfg={"Cursor": "cursor-agent"},
    )

    assert results[0].ok
    assert fake.sent_keys["%1"] == "cursor-agent"
    created = next(iter(window for window in fake.windows.values() if not window["preexisting"]))
    new_window = next(args for args in fake.commands if args[0] == "new-window")
    assert new_window[new_window.index("-c") + 1] == entry.path
    bindings = ProjectBindingStore(tmp_path / "project-bindings.json")
    bound = bindings.usable("%1", "0", "700")
    assert bound["project_path"] == entry.path
    assert bound["agent"] == "Cursor"
    assert bound["project_name"] == "tower-repo"
    assert overrides.get_agent("%1", "0", "111") is None
    assert overrides.get_agent("%1", "0", "700") is None
    assert overrides.get_project("%1", "0", "700") is None
    assert load_recent(tmp_path, "MAINPC") == [entry.path]
    assert load_recent(tmp_path, "ASUS") == []
    assert created["layout"] == "tiled"


def test_remote_create_uses_the_selected_host_and_does_not_spawn_during_browse(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher.discovery import ProjectEntry
    from tmux_agent_tower.ui import workspace_browser

    spawned = []

    def fake_remote(host, window_name, targets, agents_cfg, layout="tiled", timeout=8.0):
        spawned.append((host, targets[0].project_path, targets[0].agent_label, layout))
        return [SpawnResult(targets[0], True, "시작됨")]

    monkeypatch.setattr(workspace_browser, "spawn_remote", fake_remote)
    pick = WorkspacePick("asus", "ASUS", True, ProjectEntry("sample-repo", REMOTE_REPO, True))
    calls = {"n": 0}

    def runner(host, script, timeout):
        calls["n"] += 1
        assert script_is_read_only(script)
        return "timeout"

    page = list_remote_children("asus", pick.entry.path, runner=runner)
    assert page.error.code == "unreachable"
    assert spawned == []

    launch_workspaces(
        session="0",
        state_dir=tmp_path,
        picks=[pick],
        agent="Shell",
        placement="current",
        layout_choice="horizontal",
        window_id="@9",
        agents_cfg={"Shell": "bash"},
    )
    assert spawned == [("asus", REMOTE_REPO, "Shell", "even-horizontal")]
    assert calls["n"] == 1
    assert load_recent(tmp_path, "asus") == [REMOTE_REPO]
    assert load_recent(tmp_path, "MAINPC") == []


def test_remote_catalog_search_is_filtered_locally(monkeypatch):
    def runner(host, script, timeout):
        assert "maxdepth" in script
        assert script_is_read_only(script)
        lines = [f"repo-{index:02d}\t{REMOTE_PROJECTS}/repo-{index:02d}\t1" for index in range(25)]
        lines.append(f"notes\t{REMOTE_NOTES}\t0")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="\n".join(lines))

    entries, error = collect_remote_catalog("asus", runner=runner)
    assert error is None
    found = filter_entries(entries, "repo-24")
    assert len(entries) > 20
    assert found[0].path == f"{REMOTE_PROJECTS}/repo-24"
    assert filter_entries(entries, "notes")[0].is_git is False
