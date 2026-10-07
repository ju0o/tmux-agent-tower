from tmux_agent_tower.tmux import discovery


def test_own_pane_is_excluded_by_id_not_window_name(monkeypatch):
    # Regression: exclusion must key off pane_id, not window name -- a
    # window can be renamed or reused for something unrelated while
    # keeping an old name, and must never be excluded/matched by that name
    # alone (real bug: a window that used to host Tower, renamed away from
    # "CONTROL" or reused for something else, either wrongly hid its real
    # content or wrongly kept swallowing navigation).
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "SomeRenamedWindow", "0", "%1", "title", "bash", "/tmp", "123", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess", exclude_pane_id="%1")
    assert rows == []


def test_a_window_named_control_is_not_specially_excluded(monkeypatch):
    # The old design excluded anything in a window literally named
    # "CONTROL". That must be gone: only the exact registered pane_id is
    # excluded, regardless of what any window is named.
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "CONTROL", "0", "%1", "title", "bash", "/tmp", "123", "0", "0"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess", exclude_pane_id="%999")  # Tower is actually pane %999
    assert len(rows) == 1
    assert rows[0]["pane_id"] == "%1"


def test_no_exclude_pane_id_excludes_nothing(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "workstation-a", "0", "%1", "title", "bash", "/tmp", "123", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess")
    assert len(rows) == 1


def test_malformed_line_is_skipped(monkeypatch):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "not-enough-fields")
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert rows == []


def test_empty_tmux_output_returns_empty_list(monkeypatch):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "")
    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert rows == []


def test_dead_pane_skips_capture(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "workstation-a", "0", "%1", "title", "bash", "/tmp", "123", "1", "0"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")

    called = {"capture_pane": False}

    def fake_capture_pane(pane_id, lines=30):
        called["capture_pane"] = True
        return ["should not be called"]

    monkeypatch.setattr(discovery.capture, "capture_pane", fake_capture_pane)

    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert len(rows) == 1
    assert rows[0]["dead"] is True
    assert rows[0]["lines"] == []
    assert called["capture_pane"] is False


def test_valid_pane_parsed_correctly(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@9", "0", "workstation-a", "2", "%9", "My Title", "codex", "/home/user", "555", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {"555": "/usr/bin/codex"})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: ["hello"])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "x")

    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert len(rows) == 1
    row = rows[0]
    assert row["pane_id"] == "%9"
    assert row["command"] == "codex"
    assert row["cmdline"] == "/usr/bin/codex"
    assert row["lines"] == ["hello"]
    assert row["auto_project"] == "x"
    assert row["session"] == "sess"
    assert row["window_id"] == "@9"
    assert row["window_index"] == "0"
    assert row["window_name"] == "workstation-a"
    assert row["pane_index"] == "2"
    assert row["pane_active"] is True
