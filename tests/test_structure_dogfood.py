"""Empty session to a multi-window workspace, using Tower's structure API only.

The harness creates the isolated session. Every later step is a function
the TUI calls. No window name is used as a write target.
"""

import os
import subprocess

import pytest

from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.state.overrides import OverrideStore
from tmux_agent_tower.tmux import structure


def _tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)


def _have_tmux():
    return _tmux("-V").returncode == 0


@pytest.mark.skipif(not _have_tmux(), reason="tmux is not installed")
def test_empty_to_working_without_manual_tmux_commands(tmp_path):
    session = f"tower-dogfood-{os.getpid()}"
    created = _tmux("new-session", "-d", "-s", session, "-n", "tower")
    assert created.returncode == 0, created.stderr
    try:
        tower_window = structure.current_window_id(session)
        tower_panes = structure.panes_of_window(tower_window)
        assert tower_window and len(tower_panes) == 1
        tower_pane = tower_panes[0]
        keep_layout = _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip()

        # 1. New window. The client (session current window) stays on Tower.
        first = structure.create_window(session, name="work", cwd="/tmp")
        assert first.ok and first.window_id != tower_window
        assert structure.current_window_id(session) == tower_window

        # 2. Add a pane in that window only.
        added = structure.create_pane(first.window_id, "horizontal", cwd="/tmp")
        assert added.ok and added.window_id == first.window_id
        assert structure.current_window_id(session) == tower_window

        # 3. Second project pane, recorded like the launcher.
        second = structure.create_window(session, name="proj2", cwd="/tmp")
        assert second.ok and second.window_id not in (tower_window, first.window_id)
        structure.rename_pane(session, second.pane_id, "Cursor - proj2")
        pid = _tmux("display-message", "-p", "-t", added.pane_id, "#{pane_pid}").stdout.strip()
        bindings = ProjectBindingStore(tmp_path / "bindings.json")
        overrides = OverrideStore(tmp_path / "overrides.json")
        bindings.record(added.pane_id, session, pid, "/tmp", "proj2", "Cursor")
        overrides.set_agent(added.pane_id, "Cursor", session, pid)

        # 4. Layout of the work window only.
        assert structure.apply_layout(session, first.window_id, "even-horizontal").ok
        assert _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip() == keep_layout
        assert structure.current_window_id(session) == tower_window

        # 5. Move the added pane onto the second window.
        moved = structure.move_pane(session, added.pane_id, second.window_id)
        assert moved.ok and moved.pane_id == added.pane_id
        assert structure.pane_window_id(added.pane_id) == second.window_id
        assert bindings.usable(added.pane_id, session, pid)["project_name"] == "proj2"
        assert overrides.get_agent(added.pane_id, session, pid) == "Cursor"

        # 6. Break that moved pane into a new window. The pane id stays.
        broken = structure.break_pane(session, added.pane_id, name="split-off")
        assert broken.ok
        assert broken.pane_id == added.pane_id
        assert broken.window_id not in (tower_window, first.window_id, second.window_id)
        assert bindings.usable(added.pane_id, session, pid)["agent"] == "Cursor"
        assert overrides.get_agent(added.pane_id, session, pid) == "Cursor"
        assert structure.current_window_id(session) == tower_window

        # 7. Rename. Identity stays the window id.
        assert structure.rename_window(session, broken.window_id, "renamed").ok
        assert structure.rename_pane(session, broken.pane_id, "renamed-pane").ok
        listed = {item["window_id"]: item["window_name"] for item in structure.list_windows(session)}
        assert listed[broken.window_id] == "renamed"
        title = _tmux("display-message", "-p", "-t", broken.pane_id, "#{pane_title}").stdout.strip()
        assert title == "renamed-pane"

        # 8. Live read of that pane, the same capture the control view uses.
        screen = _tmux("capture-pane", "-p", "-t", broken.pane_id, "-S", "-5")
        assert screen.returncode == 0

        # Duplicate names do not retile each other.
        same_a = structure.create_window(session, name="same")
        same_b = structure.create_window(session, name="same")
        assert same_a.window_id != same_b.window_id
        layout_b = _tmux("display-message", "-p", "-t", same_b.window_id, "#{window_layout}").stdout.strip()
        assert structure.apply_layout(session, same_a.window_id, "tiled").ok
        assert _tmux("display-message", "-p", "-t", same_b.window_id, "#{window_layout}").stdout.strip() == layout_b

        # 9. Close the test pane. Tower's pane is refused.
        assert structure.kill_pane(session, tower_pane, own_pane_id=tower_pane).detail == "tower_pane"
        assert structure.kill_pane(session, broken.pane_id, own_pane_id=tower_pane).ok
        assert broken.pane_id not in structure.pane_ids(session)
        assert tower_pane in structure.pane_ids(session)

        # 10. Close a test window. The window that holds Tower is refused.
        assert structure.kill_window(session, tower_window, own_pane_id=tower_pane).detail == "tower_pane"
        assert structure.kill_window(session, first.window_id, own_pane_id=tower_pane).ok
        assert first.window_id not in structure.window_ids(session)
        assert tower_window in structure.window_ids(session)
        assert structure.current_window_id(session) == tower_window
        assert _tmux("display-message", "-p", "-t", tower_window, "#{window_layout}").stdout.strip() == keep_layout
    finally:
        _tmux("kill-session", "-t", session)
