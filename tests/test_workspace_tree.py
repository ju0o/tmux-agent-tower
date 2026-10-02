"""Folder tree: open one directory at a time, and keep the parents visible."""

import subprocess

from tmux_agent_tower.launcher.browse import (
    DirectoryCache,
    list_local_children,
    list_remote_children,
    remote_list_script,
    script_is_read_only,
    validate_local_path,
)
from tmux_agent_tower.launcher.discovery import ProjectEntry
from tmux_agent_tower.launcher.spawn import SpawnResult
from tmux_agent_tower.launcher.treeview import (
    FolderTree,
    TreeBook,
    command_for,
    detail_fields,
    format_row,
    visible_window,
)
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.ui.workspace_browser import WorkspacePick, launch_workspaces
from test_launcher_spawn import FakeTmux


def _git(path):
    (path / ".git").mkdir()


def _entry(path):
    return ProjectEntry(path.name, str(path), (path / ".git").exists())


def test_expand_is_lazy_and_collapse_keeps_the_parent_context(tmp_path):
    root = tmp_path / "f"
    projects = root / "Projects"
    experiments = projects / "Experiments"
    repo = experiments / "tmux-agent-tower-remote"
    other = experiments / "other-project"
    repo.mkdir(parents=True)
    other.mkdir()
    _git(repo)
    (root / "JuPortal").mkdir()
    (experiments / "repo" / "nested").mkdir(parents=True)

    fetches = []

    def fetch(path):
        fetches.append(path)
        page = list_local_children(path, include_heavy=True)
        assert page.ok
        return page

    tree = FolderTree("MAINPC", _entry(root))
    assert tree.expand_action() == "fetch"
    tree.apply_children(root.resolve().as_posix() if False else str(root), fetch(str(root)).children)
    assert fetches == [str(root)]
    assert all(child.loaded is False for child in tree.root.children)

    projects_node = next(child for child in tree.root.children if child.name == "Projects")
    tree.selected = next(index for index, row in enumerate(tree.visible_rows()) if row.node is projects_node)
    assert tree.expand_action() == "fetch"
    tree.apply_children(projects_node.path, fetch(projects_node.path).children)
    experiments_node = next(child for child in projects_node.children if child.name == "Experiments")
    tree.selected = next(index for index, row in enumerate(tree.visible_rows()) if row.node is experiments_node)
    tree.apply_children(experiments_node.path, fetch(experiments_node.path).children)

    rows = tree.visible_rows()
    names = [row.node.name for row in rows]
    assert names.index("f") < names.index("Projects") < names.index("Experiments") < names.index("tmux-agent-tower-remote")
    assert "JuPortal" in names and "nested" not in names
    by_name = {row.node.name: row.depth for row in rows}
    assert by_name["f"] < by_name["Projects"] < by_name["Experiments"] < by_name["tmux-agent-tower-remote"]
    assert "nested" not in [row.node.name for row in rows]
    assert len(fetches) == 3

    repo_row = next(row for row in rows if row.node.name == "tmux-agent-tower-remote")
    assert "◆" in format_row(repo_row)
    assert "▸" in format_row(repo_row)
    folder_row = next(row for row in rows if row.node.name == "JuPortal")
    assert "◆" not in format_row(folder_row)
    name, path, kind = detail_fields(repo_row.node)
    assert name == "tmux-agent-tower-remote"
    assert path == str(repo.resolve())
    assert kind == "Git"

    tree.selected = next(index for index, row in enumerate(tree.visible_rows()) if row.node is experiments_node)
    assert tree.collapse_action() == "collapsed"
    visible = [row.node.name for row in tree.visible_rows()]
    assert "Experiments" in visible
    assert "tmux-agent-tower-remote" not in visible
    assert "Projects" in visible
    assert tree.expand_action() == "cached"
    assert fetches == [str(root), str(projects.resolve()), str(experiments.resolve())]


def test_refresh_rereads_one_directory_and_a_large_directory_stays_one_page(tmp_path):
    root = tmp_path / "wide"
    root.mkdir()
    for index in range(200):
        (root / f"item-{index:03d}").mkdir()
    (root / "node_modules").mkdir()
    (root / ".git").mkdir()
    (root / ".hidden").mkdir()

    plain = list_local_children(str(root))
    assert "node_modules" not in {child.name for child in plain.children}
    assert ".git" not in {child.name for child in plain.children}
    heavy = validate_local_path(str(root / "node_modules"))
    assert heavy.ok and heavy.entry.path == str((root / "node_modules").resolve())

    cache = DirectoryCache()
    calls = {"n": 0}

    def fetch(path):
        calls["n"] += 1
        return list_local_children(path, include_heavy=True)

    tree = FolderTree("MAINPC", _entry(root))
    first = fetch(str(root))
    cache.put("MAINPC", first.path, first, "heavy")
    tree.apply_children(str(root.resolve()), first.children)
    assert calls["n"] == 1
    assert any(child.name == "node_modules" and child.loaded is False for child in tree.root.children)
    assert ".hidden" not in {child.name for child in tree.root.children}
    assert len(visible_window(tree.visible_rows(), 0, 8)) == 8
    assert len(tree.visible_rows()) > 8

    (root / "added-later").mkdir()
    cache.drop("MAINPC", str(root.resolve()), "heavy")
    again = fetch(str(root.resolve()))
    tree.apply_children(str(root.resolve()), again.children, preserve=True)
    assert calls["n"] == 2
    assert any(child.name == "added-later" for child in tree.root.children)
    assert all(not child.loaded for child in tree.root.children)


def test_filter_keeps_ancestors_and_keys_do_not_select_on_enter():
    root = TreeNode_from("f", "/mnt/f", False)
    projects = TreeNode_from("Projects", "/mnt/f/Projects", False)
    repo = TreeNode_from("demo", "/mnt/f/Projects/demo", True)
    root.children = [projects]
    root.expanded = True
    root.loaded = True
    projects.children = [repo]
    projects.expanded = True
    projects.loaded = True
    tree = FolderTree("MAINPC", ProjectEntry("f", "/mnt/f", False))
    tree.root = root
    tree.query = "demo"
    names = [row.node.name for row in tree.visible_rows()]
    assert names == ["f", "Projects", "demo"]
    assert command_for("enter") == "expand"
    assert command_for("space") == "select"
    assert command_for("enter") != command_for("space")


def TreeNode_from(name, path, is_git):
    from tmux_agent_tower.launcher.treeview import TreeNode

    return TreeNode(name, path, is_git)


def test_hosts_keep_separate_trees_and_ssh_expand_is_cached():
    book = TreeBook()
    local = FolderTree("MAINPC", ProjectEntry("f", "/mnt/f", False))
    remote = FolderTree("asus", ProjectEntry("skkse12", "/home/skkse12", False))
    book.remember("MAINPC", local)
    book.remember("asus", remote)
    local.root.expanded = True
    assert book.recall("asus").root.expanded is False
    assert book.cache_for("MAINPC") is not book.cache_for("asus")

    calls = []

    def runner(host, script, timeout):
        calls.append(script)
        assert script_is_read_only(script)
        assert "maxdepth 1" in script
        assert "tmux" not in script
        if "/home/skkse12/projects" in script:
            stdout = "\n".join([
                "__STATUS__ ok",
                "__PATH__ /home/skkse12/projects",
                "__GIT__ 0",
                "JuAgentEconomy\t/home/skkse12/projects/JuAgentEconomy\t0",
            ])
        else:
            stdout = "\n".join([
                "__STATUS__ ok",
                "__PATH__ /home/skkse12",
                "__GIT__ 0",
                "projects\t/home/skkse12/projects\t0",
            ])
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)

    cache = book.cache_for("asus")
    home = list_remote_children("asus", "/home/skkse12", cache=cache, runner=runner, include_heavy=True)
    remote.apply_children("/home/skkse12", home.children)
    child = remote.root.children[0]
    remote.selected = 1
    assert remote.expand_action() == "fetch"
    page = list_remote_children("asus", child.path, cache=cache, runner=runner, include_heavy=True)
    remote.apply_children(child.path, page.children)
    remote.selected = 1
    assert remote.collapse_action() == "collapsed"
    assert remote.expand_action() == "cached"
    assert len(calls) == 2
    assert "JuAgentEconomy" in [row.node.name for row in remote.visible_rows()]
    assert book.cache_for("MAINPC").get("asus", "/home/skkse12", "heavy") is None


def test_create_uses_the_selected_tree_directory(tmp_path, monkeypatch):
    from tmux_agent_tower.launcher import spawn

    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo)
    tree = FolderTree("MAINPC", _entry(tmp_path))
    page = list_local_children(str(tmp_path), include_heavy=True)
    tree.apply_children(str(tmp_path.resolve()), page.children)
    tree.selected = next(index for index, row in enumerate(tree.visible_rows()) if row.node.name == "demo")
    entry = tree.as_entry()

    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))
    results = launch_workspaces(
        session="0",
        state_dir=tmp_path / "state",
        picks=[WorkspacePick("MAINPC", "MAINPC", False, entry)],
        agent="Cursor",
        placement="new",
        layout_choice="auto",
        agents_cfg={"Cursor": "cursor-agent"},
    )
    assert results[0].ok
    bound = ProjectBindingStore(tmp_path / "state" / "project-bindings.json").usable("%1", "0", "700")
    assert bound["project_path"] == str(repo.resolve())
    assert bound["agent"] == "Cursor"
    assert fake.sent_keys["%1"] == "cursor-agent"


def test_remote_create_is_not_part_of_expand(monkeypatch, tmp_path):
    from tmux_agent_tower.ui import workspace_browser

    spawned = []
    monkeypatch.setattr(
        workspace_browser,
        "spawn_remote",
        lambda host, window_name, targets, agents_cfg, layout="tiled", timeout=8.0: spawned.append(host) or [SpawnResult(targets[0], True, "시작됨")],
    )
    script = remote_list_script("/home/skkse12", include_heavy=True)
    assert script_is_read_only(script)
    assert "node_modules" not in script.split("case", 1)[-1] or ".git|.venv" in script
    launch_workspaces(
        session="0",
        state_dir=tmp_path,
        picks=[WorkspacePick("asus", "ASUS", True, ProjectEntry("code", "/home/skkse12/projects/JuAgentEconomy/code", False))],
        agent="Shell",
        placement="new",
        layout_choice="auto",
        agents_cfg={"Shell": "bash"},
    )
    assert spawned == ["asus"]
