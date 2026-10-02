"""Live browse and create. Local work stays in a detached session.

ASUS browse is read-only. A remote create adds one window and then
removes only that window.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tmux_agent_tower.launcher.browse import (
    collect_remote_catalog,
    list_local_children,
    list_remote_children,
    remote_list_script,
    remote_start_roots,
    script_is_read_only,
    validate_local_path,
    validate_remote_path,
)
from tmux_agent_tower.launcher.config import default_agent_commands
from tmux_agent_tower.launcher.discovery import find_git_projects
from tmux_agent_tower.launcher.spawn import SpawnTarget
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.ui.workspace_browser import WorkspacePick, launch_workspaces


def _tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)


def _have_tmux():
    return _tmux("-V").returncode == 0


def _session_windows(session: str) -> str:
    return _tmux("list-windows", "-t", session, "-F", "#{window_id}").stdout


REPO = Path("/home/user/Projects/Experiments/tmux-agent-tower-remote")


@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_local_tree_and_direct_path_create_in_an_isolated_session(tmp_path):
    assert REPO.is_dir()
    parent = list_local_children(str(REPO.parent))
    match = [child for child in parent.children if child.path == str(REPO.resolve())]
    assert match and match[0].is_git and match[0].name == REPO.name

    plain = tmp_path / "not-in-the-discovery-list"
    plain.mkdir()
    direct = validate_local_path(str(plain))
    assert direct.ok and direct.entry.is_git is False
    assert find_git_projects([str(REPO.parent)])
    assert all(entry.path != direct.entry.path for entry in find_git_projects([str(REPO.parent)]))

    user_before = _session_windows("0")
    session = f"tower-browser-{os.getpid()}"
    created = _tmux("new-session", "-d", "-s", session, "-n", "tower")
    assert created.returncode == 0, created.stderr
    tower_window = _tmux("display-message", "-p", "-t", session, "#{window_id}").stdout.strip()
    try:
        results = launch_workspaces(
            session=session,
            state_dir=tmp_path / "state",
            picks=[WorkspacePick("MAINPC", "MAINPC", False, match[0])],
            agent="Shell",
            placement="new",
            layout_choice="auto",
            agents_cfg=default_agent_commands(),
        )
        assert results[0].ok
        windows = _tmux(
            "list-panes", "-s", "-t", session,
            "-F", "#{window_id}\t#{pane_id}\t#{pane_current_path}",
        ).stdout
        repo_rows = [
            line for line in windows.splitlines()
            if str(REPO.resolve()) in line and not line.startswith(tower_window + "\t")
        ]
        assert repo_rows
        pane_id = repo_rows[0].split("\t")[1]
        pid = _tmux("display-message", "-p", "-t", pane_id, "#{pane_pid}").stdout.strip()
        bound = ProjectBindingStore(tmp_path / "state" / "project-bindings.json").usable(pane_id, session, pid)
        assert bound["project_path"] == str(REPO.resolve())
        assert bound["agent"] == "Shell"
        assert bound["project_name"] == REPO.name

        direct_result = launch_workspaces(
            session=session,
            state_dir=tmp_path / "state",
            picks=[WorkspacePick("MAINPC", "MAINPC", False, direct.entry)],
            agent="Shell",
            placement="new",
            layout_choice="horizontal",
            agents_cfg=default_agent_commands(),
        )
        assert direct_result[0].ok
        again = _tmux("list-panes", "-s", "-t", session, "-F", "#{pane_current_path}").stdout
        assert direct.entry.path in again.splitlines()
    finally:
        _tmux("kill-session", "-t", session)
    assert _session_windows("0") == user_before


def _asus_up() -> bool:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "asus", "true"],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _asus(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "asus", script],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )


@pytest.mark.skipif(not _asus_up(), reason="ASUS SSH is not reachable")
def test_asus_browse_then_one_new_window_only():
    before = _asus('tmux list-windows -a -F "#{window_id}" 2>/dev/null || true').stdout
    roots, error = remote_start_roots("asus", [], timeout=8)
    assert error is None
    assert roots
    home = next(path for path in roots if path)
    page = list_remote_children("asus", home, timeout=8)
    assert page.ok, page.error
    during = _asus('tmux list-windows -a -F "#{window_id}" 2>/dev/null || true').stdout
    assert during == before

    catalog, catalog_error = collect_remote_catalog("asus", timeout=12)
    assert catalog_error is None
    chosen = next((entry for entry in catalog if entry.is_git), None)
    if chosen is None and page.children:
        chosen = page.children[0]
    if chosen is None:
        checked = validate_remote_path("asus", home)
        assert checked.ok
        chosen = checked.entry
    checked = validate_remote_path("asus", chosen.path)
    assert checked.ok
    chosen = checked.entry
    assert script_is_read_only(remote_list_script(chosen.path))

    from tmux_agent_tower.launcher.spawn import spawn_remote

    results = spawn_remote(
        "asus",
        "tower-browser-dogfood",
        [SpawnTarget(chosen.path, chosen.name, "Shell")],
        {"Shell": "bash"},
        timeout=15,
    )
    assert results and results[0].ok, results[0].detail if results else "no result"
    after = set(_asus('tmux list-windows -a -F "#{window_id}"').stdout.split())
    before_ids = set(before.split())
    created_ids = after - before_ids
    window_id = next(iter(created_ids)) if len(created_ids) == 1 else ""
    try:
        assert len(created_ids) == 1
        assert re.fullmatch(r"@\d+", window_id)
        info = _asus(
            f'tmux list-panes -t {window_id} -F "#{{pane_current_path}}\t#{{pane_current_command}}"'
        ).stdout.strip()
        path, _, command = info.partition("\t")
        assert path == chosen.path
        assert command in ("bash", "sh", "zsh")
    finally:
        if re.fullmatch(r"@\d+", window_id or ""):
            killed = _asus(f"tmux kill-window -t {window_id}")
            assert killed.returncode == 0, killed.stderr
    restored = set(_asus('tmux list-windows -a -F "#{window_id}"').stdout.split())
    assert restored == before_ids
