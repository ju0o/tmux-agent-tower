from tmux_agent_tower.tmux import discovery


def test_control_window_is_excluded(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "0", "CONTROL", "0", "%1", "title", "bash", "/tmp", "123", "0"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess", "CONTROL")
    assert rows == []


def test_malformed_line_is_skipped(monkeypatch):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "not-enough-fields")
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    rows = discovery.list_panes("sess", "CONTROL")
    assert rows == []


def test_empty_tmux_output_returns_empty_list(monkeypatch):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "")
    rows = discovery.list_panes("sess", "CONTROL")
    assert rows == []


def test_dead_pane_skips_capture(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "0", "MAINPC", "0", "%1", "title", "bash", "/tmp", "123", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")

    called = {"capture_pane": False}

    def fake_capture_pane(pane_id, lines=30):
        called["capture_pane"] = True
        return ["should not be called"]

    monkeypatch.setattr(discovery.capture, "capture_pane", fake_capture_pane)

    rows = discovery.list_panes("sess", "CONTROL")
    assert len(rows) == 1
    assert rows[0]["dead"] is True
    assert rows[0]["lines"] == []
    assert called["capture_pane"] is False


def test_valid_pane_parsed_correctly(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "0", "MAINPC", "2", "%9", "My Title", "codex", "/home/x", "555", "0"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {"555": "/usr/bin/codex"})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: ["hello"])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "x")

    rows = discovery.list_panes("sess", "CONTROL")
    assert len(rows) == 1
    row = rows[0]
    assert row["pane_id"] == "%9"
    assert row["command"] == "codex"
    assert row["cmdline"] == "/usr/bin/codex"
    assert row["lines"] == ["hello"]
    assert row["auto_project"] == "x"
