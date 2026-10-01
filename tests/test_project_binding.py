"""Project identity: launcher binding and agent cwd, never a guessed title."""

from tmux_agent_tower.detection.identity import agent_process_cwd
from tmux_agent_tower.launcher import spawn
from tmux_agent_tower.launcher.spawn import SpawnTarget
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.ui import render


class _FakeTmux:
    def __init__(self):
        self.windows = {}
        self._next = 1
        self.pids = {}

    def run_tmux(self, args, capture=True, timeout=None):
        cmd = args[0]
        if cmd == "list-windows":
            return "\n".join(self.windows)
        if cmd == "list-panes":
            window = args[args.index("-t") + 1].split(":", 1)[1]
            return "\n".join(self.windows.get(window, []))
        if cmd == "new-window":
            name = args[args.index("-n") + 1]
            pane = f"%{self._next}"
            self._next += 1
            self.windows[name] = [pane]
            self.pids[pane] = "4242"
            return ""
        if cmd == "display-message":
            pane = args[args.index("-t") + 1]
            return self.pids.get(pane, "")
        if cmd in ("send-keys", "select-pane", "select-layout", "split-window"):
            if cmd == "split-window":
                window = args[args.index("-t") + 1].split(":", 1)[1]
                pane = f"%{self._next}"
                self._next += 1
                self.windows.setdefault(window, []).append(pane)
                self.pids[pane] = "5252"
            return ""
        return ""


def test_launcher_records_a_project_binding(tmp_path, monkeypatch):
    project = tmp_path / "Agent-Relay"
    project.mkdir()
    fake = _FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))
    store = ProjectBindingStore(tmp_path / "project-bindings.json")

    spawn.spawn_local(
        "0",
        "MAINPC",
        [SpawnTarget(str(project), "Agent-Relay", "Claude")],
        {"Claude": "claude"},
        bindings=store,
    )

    record = store.usable("%1", "0", "4242")
    assert record is not None
    assert record["project_path"] == str(project)
    assert record["project_name"] == "Agent-Relay"
    assert record["agent"] == "Claude"
    assert record["session"] == "0"


def test_agent_cwd_is_not_a_tool_shell_cwd():
    cmdline = {"1": "-bash", "2": "/usr/bin/codex", "3": "bash -c true"}
    ppid = {"1": "0", "2": "1", "3": "2"}
    cwds = {"2": "/work/Agent-Relay", "3": "/tmp/unrelated"}
    assert agent_process_cwd("1", cmdline, ppid, read_cwd=cwds.get) == "/work/Agent-Relay"


def test_process_cwd_repo_beats_tmux_cwd_and_title():
    name, source = render.resolve_project_identity(
        None, None, "Claude Tmux Control", "f", "HOST", "(이름 없음)",
        process_git_name="Agent-Relay",
    )
    assert (name, source) == ("Agent-Relay", "process")


def test_tmux_cwd_git_is_the_fallback_when_the_agent_has_no_repo():
    name, source = render.resolve_project_identity(
        None, "from-pane", "Some task title", "f", "HOST", "(이름 없음)",
    )
    assert (name, source) == ("from-pane", "git")


def test_binding_beats_process_cwd():
    name, source = render.resolve_project_identity(
        None, "pane-repo", "Some task title", "f", "HOST", "(이름 없음)",
        binding_name="Agent-Relay",
        process_git_name="Other",
    )
    assert (name, source) == ("Agent-Relay", "binding")


def test_override_beats_binding():
    name, source = render.resolve_project_identity(
        "Saved", None, "Claude Tmux Control", "f", "HOST", "(이름 없음)",
        binding_name="Agent-Relay",
        process_git_name="Other",
    )
    assert (name, source) == ("Saved", "override")


def test_stale_binding_is_ignored_on_pid_or_session_mismatch(tmp_path):
    project = tmp_path / "Agent-Relay"
    project.mkdir()
    store = ProjectBindingStore(tmp_path / "project-bindings.json")
    store.record("%7", "0", "4242", str(project), "Agent-Relay", "Claude")
    assert store.usable("%7", "0", "9999") is None
    assert store.usable("%7", "1", "4242") is None
    assert store.usable("%7", "0", "4242") is not None


def test_missing_project_path_is_not_shown(tmp_path):
    gone = tmp_path / "missing"
    store = ProjectBindingStore(tmp_path / "project-bindings.json")
    store.record("%7", "0", "4242", str(gone), "missing", "Claude")
    assert store.usable("%7", "0", "4242") is None


def test_binding_without_a_pid_is_not_stored(tmp_path):
    store = ProjectBindingStore(tmp_path / "project-bindings.json")
    store.record("%7", "0", "", str(tmp_path), "x", "Claude")
    assert store.usable("%7", "0", "") is None


def test_agent_title_and_unknown_directory_stay_unnamed():
    name, source = render.resolve_project_identity(
        None, None, "Claude Tmux Control", "f", "HOST", "(이름 없음)",
    )
    assert (name, source) == ("(이름 없음)", "none")
