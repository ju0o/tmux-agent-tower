"""A reused pane id must not wear the previous pane's CommandCode label.

The live screen showed Project SamplePortal / Agent CommandCode while the
process in that pane was cursor-agent, and the sense line said
override→process. The override belonged to an older pid.
"""

from tmux_agent_tower.tmux import discovery
from tmux_agent_tower.ui import tower as tower_module


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")


def _line(pid: str, command: str = "node") -> str:
    return discovery.FIELD_SEP.join(
        ["main", "@4", "1", "work", "0", "%12", "cursor-agent", command, "/tmp/proj", pid, "0", "1"]
    )


def test_stale_override_loses_to_the_live_cursor_process(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args, capture=True, timeout=3.0: _line("222"))
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(
        discovery.process_detection,
        "cmdline_by_pid",
        lambda: {"222": "/opt/cursor-agent/index.js"},
    )
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: None)
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    tower = tower_module.Tower("main", own_pane_id="%1")
    tower.overrides.set_project("%12", "SamplePortal", "main", "111")
    tower.overrides.set_agent("%12", "CommandCode", "main", "111")
    tower.overrides.set_role("%12", "builder", "main", "111")
    tower.load()

    row = next(item for item in tower.rows if item["pane_id"] == "%12")
    assert row["agent"] == "Cursor"
    assert row["role"] is None
    assert row["agent_source"] == "process"
    assert row["project"] != "SamplePortal"
    assert row["project_source"] != "override"
    assert "override" not in row["agent_source"]


def test_fresh_override_on_the_current_pid_still_wins(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args, capture=True, timeout=3.0: _line("222"))
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(
        discovery.process_detection,
        "cmdline_by_pid",
        lambda: {"222": "/opt/cursor-agent/index.js"},
    )
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: None)
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    tower = tower_module.Tower("main", own_pane_id="%1")
    tower.overrides.set_project("%12", "SamplePortal", "main", "222")
    tower.overrides.set_agent("%12", "CommandCode", "main", "222")
    tower.overrides.set_role("%12", "reviewer", "main", "222")
    tower.load()

    row = next(item for item in tower.rows if item["pane_id"] == "%12")
    assert row["project"] == "SamplePortal"
    assert row["agent"] == "CommandCode"
    assert row["role"] == "reviewer"
    assert row["agent_source"] == "override"
    assert row["auto_agent"] == "Cursor"
    assert row["auto_agent_source"] == "process"


def test_commandcode_label_still_reads_the_ssh_screen(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    screen = [
        "Short answer for the test.",
        "✻ Worked for 5s",
        "❯",
        "⏵⏵ auto mode on (shift+tab to cycle)",
    ]
    monkeypatch.setattr(
        discovery.capture,
        "run_tmux",
        lambda args, capture=True, timeout=3.0: discovery.FIELD_SEP.join(
            ["main", "@4", "1", "work", "0", "%12", "ssh", "ssh", "/tmp/proj", "222", "0", "1"]
        ),
    )
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: list(screen))
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: None)
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    tower = tower_module.Tower("main", own_pane_id="%1")
    tower.overrides.set_agent("%12", "CommandCode", "main", "222")
    tower.load()
    row = next(item for item in tower.rows if item["pane_id"] == "%12")
    assert row["agent"] == "CommandCode"
    assert row["agent_source"] == "override"
    assert row["status"] == "IDLE"
    assert row["attention"] == "none"


def test_pane_id_reuse_does_not_keep_the_old_label(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    pid = {"value": "111"}

    def fake_run(args, capture=True, timeout=3.0):
        return _line(pid["value"])

    monkeypatch.setattr(discovery.capture, "run_tmux", fake_run)
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: None)
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    tower = tower_module.Tower("main", own_pane_id="%1")
    tower.overrides.set_agent("%12", "CommandCode", "main", "111")
    tower.overrides.set_role("%12", "builder", "main", "111")
    tower.load()
    assert tower.rows[0]["agent"] == "CommandCode"
    assert tower.rows[0]["role"] == "builder"

    pid["value"] = "333"
    tower.overrides._cache = None
    tower.load()
    assert tower.rows[0]["agent"] != "CommandCode"
    assert tower.rows[0]["role"] is None
    assert tower.rows[0]["agent_source"] != "override"
