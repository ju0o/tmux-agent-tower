import subprocess

from tmux_agent_tower.launcher import spawn
from tmux_agent_tower.launcher.spawn import SpawnTarget


class FakeTmux:
    """Minimal fake of the local tmux server used by spawn_local's tests."""

    def __init__(self):
        self.windows = {}  # window_name -> list of pane_ids
        self._next_pane = 1
        self.titles = {}
        self.layouts = {}
        self.sent_keys = {}

    def run_tmux(self, args, capture=True, timeout=None):
        cmd = args[0]

        if cmd == "list-windows":
            return "\n".join(self.windows.keys())

        if cmd == "list-panes":
            target = args[args.index("-t") + 1]
            window_name = target.split(":", 1)[1]
            return "\n".join(self.windows.get(window_name, []))

        if cmd == "new-window":
            window_name = args[args.index("-n") + 1]
            pane_id = f"%{self._next_pane}"
            self._next_pane += 1
            self.windows[window_name] = [pane_id]
            return ""

        if cmd == "split-window":
            target = args[args.index("-t") + 1]
            window_name = target.split(":", 1)[1]
            pane_id = f"%{self._next_pane}"
            self._next_pane += 1
            self.windows.setdefault(window_name, []).append(pane_id)
            return ""

        if cmd == "send-keys":
            pane_id = args[args.index("-t") + 1]
            self.sent_keys[pane_id] = args[args.index("-t") + 2]
            return ""

        if cmd == "select-pane":
            pane_id = args[args.index("-t") + 1]
            title = args[args.index("-T") + 1]
            self.titles[pane_id] = title
            return ""

        if cmd == "select-layout":
            target = args[args.index("-t") + 1]
            layout = args[-1]
            self.layouts[target] = layout
            return ""

        return ""


def test_spawn_local_creates_new_window_when_missing(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)

    targets = [SpawnTarget("/proj/a", "a", "Codex")]
    agents = {"Codex": "codex"}
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))

    results = spawn.spawn_local("0", "MAINPC", targets, agents)

    assert len(results) == 1
    assert results[0].ok is True
    assert "MAINPC" in fake.windows
    assert len(fake.windows["MAINPC"]) == 1


def test_spawn_local_appends_to_existing_window_without_touching_it(monkeypatch):
    fake = FakeTmux()
    fake.windows["MAINPC"] = ["%1"]  # pre-existing pane from before this call
    fake._next_pane = 2
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(spawn, "resolve_agent_command", lambda label, cfg: cfg.get(label))

    targets = [SpawnTarget("/proj/b", "b", "Claude")]
    spawn.spawn_local("0", "MAINPC", targets, {"Claude": "claude"})

    assert fake.windows["MAINPC"] == ["%1", "%2"]
    # The pre-existing pane was never sent keys or retitled.
    assert "%1" not in fake.sent_keys
    assert "%1" not in fake.titles


def test_spawn_local_missing_command_does_not_abort_remaining_targets(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    monkeypatch.setattr(
        spawn, "resolve_agent_command",
        lambda label, cfg: None if label == "OpenCode" else cfg.get(label),
    )

    targets = [
        SpawnTarget("/proj/a", "a", "OpenCode"),
        SpawnTarget("/proj/b", "b", "Codex"),
    ]
    results = spawn.spawn_local("0", "MAINPC", targets, {"OpenCode": "opencode", "Codex": "codex"})

    assert results[0].ok is False
    assert "찾을 수 없습니다" in results[0].detail
    assert results[1].ok is True
    # Both panes were still created.
    assert len(fake.windows["MAINPC"]) == 2


def test_spawn_local_empty_targets_is_noop(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(spawn.tmux_capture, "run_tmux", fake.run_tmux)
    assert spawn.spawn_local("0", "MAINPC", [], {}) == []
    assert fake.windows == {}


def test_build_remote_script_quotes_paths_with_spaces():
    targets = [(SpawnTarget("/proj/has space", "p", "Codex"), "codex")]
    script = spawn.build_remote_script("MAINPC", targets, "tiled")
    assert "'/proj/has space'" in script


def test_build_remote_script_new_session_uses_new_session_n_not_new_window():
    # Regression: creating a session with plain `new-session -d -s tower`
    # followed by a separate `new-window` leaves tmux's own default first
    # window behind. The NEW_SESSION branch must fold window creation into
    # `new-session -n` itself so nothing meaningless is left over.
    targets = [(SpawnTarget("/proj/a", "a", "Codex"), "codex")]
    script = spawn.build_remote_script("ASUS", targets, "tiled")
    assert 'new-session -d -s "$SESS" -n "$WIN"' in script
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
