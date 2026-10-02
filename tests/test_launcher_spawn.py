import subprocess

from tmux_agent_tower.launcher import spawn
from tmux_agent_tower.launcher.spawn import SpawnTarget


class FakeTmux:
    """Records window ids. A pre-existing window is never a spawn target."""

    def __init__(self):
        self.windows = {}  # window_id -> {"name", "panes", "layout", "preexisting"}
        self._next_window = 1
        self._next_pane = 1
        self.titles = {}
        self.sent_keys = {}
        self.commands = []
        self.pids = {}

    def add_existing(self, name: str, panes: list, layout: str = "even-horizontal") -> str:
        window_id = f"@{self._next_window}"
        self._next_window += 1
        self.windows[window_id] = {
            "name": name,
            "panes": list(panes),
            "layout": layout,
            "preexisting": True,
        }
        return window_id

    def run_tmux(self, args, capture=True, timeout=None):
        self.commands.append(list(args))
        cmd = args[0]

        if cmd == "new-window":
            assert "-d" in args
            name = args[args.index("-n") + 1]
            window_id = f"@{self._next_window}"
            pane_id = f"%{self._next_pane}"
            self._next_window += 1
            self._next_pane += 1
            self.windows[window_id] = {
                "name": name,
                "panes": [pane_id],
                "layout": None,
                "preexisting": False,
            }
            self.pids[pane_id] = "700"
            return f"{window_id}\t{pane_id}"

        if cmd == "split-window":
            assert "-d" in args
            window_id = args[args.index("-t") + 1]
            pane_id = f"%{self._next_pane}"
            self._next_pane += 1
            self.windows[window_id]["panes"].append(pane_id)
            self.pids[pane_id] = "701"
            return pane_id

        if cmd == "display-message":
            pane_id = args[args.index("-t") + 1]
            return self.pids.get(pane_id, "")

        if cmd == "send-keys":
            pane_id = args[args.index("-t") + 1]
            self.sent_keys[pane_id] = args[args.index("-t") + 2]
            return ""

        if cmd == "select-pane":
            pane_id = args[args.index("-t") + 1]
            self.titles[pane_id] = args[args.index("-T") + 1]
            return ""

        if cmd == "select-layout":
            window_id = args[args.index("-t") + 1]
            self.windows[window_id]["layout"] = args[-1]
            return ""

        if cmd in ("switch-client", "select-window"):
            raise AssertionError(cmd)

        return ""


def _new_windows(fake: FakeTmux):
    return {wid: win for wid, win in fake.windows.items() if not win["preexisting"]}


def test_n_always_opens_a_new_window(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))

    results = spawn.spawn_local("0", [SpawnTarget("/proj/a", "Agent-Relay", "Codex")], {"Codex": "codex"})

    assert results[0].ok is True
    created = _new_windows(fake)
    assert len(created) == 1
    window = next(iter(created.values()))
    assert window["name"] == "Agent-Relay"
    assert window["panes"] == ["%1"]
    assert window["layout"] == "tiled"


def test_same_name_existing_window_is_not_split_or_retiled(monkeypatch):
    fake = FakeTmux()
    old_id = fake.add_existing("Agent-Relay", ["%9"], layout="main-vertical")
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))

    spawn.spawn_local("0", [SpawnTarget("/proj/a", "Agent-Relay", "Codex")], {"Codex": "codex"})

    assert fake.windows[old_id]["panes"] == ["%9"]
    assert fake.windows[old_id]["layout"] == "main-vertical"
    assert "%9" not in fake.sent_keys
    assert "%9" not in fake.titles
    new_id = next(iter(_new_windows(fake)))
    assert new_id != old_id
    assert fake.windows[new_id]["layout"] == "tiled"
    assert all(args[args.index("-t") + 1] != old_id for args in fake.commands if args[0] == "select-layout")


def test_w_splits_only_inside_the_new_window(monkeypatch):
    fake = FakeTmux()
    old_id = fake.add_existing("Tower Workspace", ["%3", "%4"], layout="main-vertical")
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))

    results = spawn.spawn_local(
        "0",
        [SpawnTarget("/proj/a", "Alpha", "Codex"), SpawnTarget("/proj/b", "Beta", "Claude")],
        {"Codex": "codex", "Claude": "claude"},
        layout="even-horizontal",
    )

    assert [r.ok for r in results] == [True, True]
    assert fake.windows[old_id]["panes"] == ["%3", "%4"]
    assert fake.windows[old_id]["layout"] == "main-vertical"
    created = _new_windows(fake)
    assert len(created) == 1
    window_id, window = next(iter(created.items()))
    assert window["name"] == "Tower Workspace"
    assert window["panes"] == ["%1", "%2"]
    assert window["layout"] == "even-horizontal"
    split_targets = [args[args.index("-t") + 1] for args in fake.commands if args[0] == "split-window"]
    assert split_targets == [window_id]


def test_spawn_drops_a_stale_override_for_the_reused_pane_id(tmp_path, monkeypatch):
    from tmux_agent_tower.state.overrides import OverrideStore

    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))
    store = OverrideStore(tmp_path / "overrides.json")
    store.set_agent("%1", "CommandCode", "0", "111")
    store.set_project("%1", "JuPortal", "0", "111")

    spawn.spawn_local(
        "0",
        [SpawnTarget("/proj/a", "Agent-Relay", "Codex")],
        {"Codex": "codex"},
        overrides=store,
    )

    assert store.get_agent("%1", "0", "111") is None
    assert store.get_agent("%1", "0", "700") is None
    assert store.get_project("%1", "0", "700") is None


def test_spawn_uses_returned_pane_id_for_binding(tmp_path, monkeypatch):
    from tmux_agent_tower.state.bindings import ProjectBindingStore

    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))
    store = ProjectBindingStore(tmp_path / "project-bindings.json")
    project = tmp_path / "Agent-Relay"
    project.mkdir()

    spawn.spawn_local(
        "0",
        [SpawnTarget(str(project), "Agent-Relay", "Shell")],
        {"Shell": "bash"},
        bindings=store,
    )

    record = store.usable("%1", "0", "700")
    assert record is not None
    assert record["project_name"] == "Agent-Relay"
    assert record["pane_pid"] == "700"


def test_missing_command_does_not_touch_existing_windows(monkeypatch):
    fake = FakeTmux()
    old_id = fake.add_existing("MAINPC", ["%8"])
    before = list(fake.windows[old_id]["panes"])
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(
        spawn, "resolve_agent_command",
        lambda label, cfg: None if label == "OpenCode" else cfg.get(label),
    )

    results = spawn.spawn_local(
        "0",
        [SpawnTarget("/proj/a", "Alpha", "OpenCode"), SpawnTarget("/proj/b", "Beta", "Codex")],
        {"OpenCode": "opencode", "Codex": "codex"},
    )

    assert results[0].ok is False
    assert results[1].ok is True
    assert fake.windows[old_id]["panes"] == before
    assert "%8" not in fake.sent_keys
    created = next(iter(_new_windows(fake).values()))
    assert len(created["panes"]) == 2


def test_new_window_and_split_do_not_move_the_client(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))

    spawn.spawn_local(
        "0",
        [SpawnTarget("/a", "Alpha", "Codex"), SpawnTarget("/b", "Beta", "Claude")],
        {"Codex": "codex", "Claude": "claude"},
    )

    created = [args for args in fake.commands if args[0] in ("new-window", "split-window")]
    assert created
    assert all("-d" in args for args in created)
    assert not any(args[0] in ("switch-client", "select-window") for args in fake.commands)


def test_spawn_local_empty_targets_is_noop(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    assert spawn.spawn_local("0", [], {}) == []
    assert fake.commands == []


def test_build_remote_script_quotes_paths_with_spaces():
    targets = [(SpawnTarget("/proj/has space", "p", "Codex"), "codex")]
    script = spawn.build_remote_script("MAINPC", targets, "tiled")
    assert "'/proj/has space'" in script


def test_build_remote_script_never_reuses_a_window_by_name():
    targets = [
        (SpawnTarget("/proj/a", "Alpha", "Codex"), "codex"),
        (SpawnTarget("/proj/b", "Beta", "Claude"), "claude"),
    ]
    script = spawn.build_remote_script("ASUS", targets, "tiled")
    assert "grep -Fxq" not in script
    assert 'split-window -t "${SESS}:${WIN}"' not in script
    assert 'new-window -d -P -F "#{window_id}"' in script
    assert 'new-session -d -P -F "#{window_id}"' in script
    assert 'split-window -d -P -F "#{pane_id}" -t "$WIN_ID"' in script
    assert 'select-layout -t "$WIN_ID"' in script
    assert "NEW_SESSION=1" in script


def test_spawn_remote_parses_ok_and_missing(monkeypatch):
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="OK 0\nMISSING 1\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    targets = [
        SpawnTarget("/proj/a", "a", "Codex"),
        SpawnTarget("/proj/b", "b", "OpenCode"),
    ]
    results = spawn.spawn_remote("asus", "ASUS", targets, {"Codex": "codex", "OpenCode": "opencode"})

    assert results[0].ok is True
    assert results[1].ok is False


def test_spawn_remote_ssh_timeout_degrades_all_targets(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    targets = [SpawnTarget("/proj/a", "a", "Codex")]
    results = spawn.spawn_remote("nonexistent-host", "ASUS", targets, {"Codex": "codex"})

    assert results[0].ok is False
    assert "연결할 수 없습니다" in results[0].detail


def test_spawn_remote_nonzero_exit_degrades_all_targets(monkeypatch):
    fake_result = subprocess.CompletedProcess(args=[], returncode=255, stdout="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_result)

    targets = [SpawnTarget("/proj/a", "a", "Codex")]
    results = spawn.spawn_remote("asus", "ASUS", targets, {"Codex": "codex"})

    assert results[0].ok is False


def test_spawn_remote_empty_targets_is_noop():
    assert spawn.spawn_remote("asus", "ASUS", [], {}) == []
