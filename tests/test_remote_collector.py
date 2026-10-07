import subprocess

import pytest

from tmux_agent_tower.remote import collector


def test_parse_snapshot_no_tmux_installed():
    result = collector.parse_remote_snapshot("HOST", "__TOWER_NO_TMUX__")
    assert result["status"] == collector.HOST_STATUS_UNKNOWN
    assert result["panes"] == []


def test_parse_snapshot_no_server_running_is_online_empty():
    result = collector.parse_remote_snapshot("HOST", "__TOWER_NO_SERVER__")
    assert result["status"] == collector.HOST_STATUS_ONLINE
    assert result["panes"] == []


def test_parse_snapshot_empty_output_is_unknown():
    result = collector.parse_remote_snapshot("HOST", "")
    assert result["status"] == collector.HOST_STATUS_UNKNOWN


def test_parse_snapshot_valid_line():
    line = "\x1f".join(["sess", "$1", "@1", "0", "win", "123456", "0", "%1", "title", "codex", "/home/user", "55", "0"])
    result = collector.parse_remote_snapshot("HOST", line)
    assert result["status"] == collector.HOST_STATUS_ONLINE
    assert len(result["panes"]) == 1
    assert result["panes"][0]["pane_id"] == "%1"
    assert result["panes"][0]["session_id"] == "$1"
    assert result["panes"][0]["window_id"] == "@1"
    assert result["panes"][0]["window_created"] == "123456"
    assert result["panes"][0]["pane_pid"] == "55"


def test_parse_snapshot_accepts_older_peer_without_window_creation_marker():
    line = "\x1f".join(["sess", "$1", "@1", "0", "win", "0", "%1", "title", "codex", "/home/user", "55", "0"])
    result = collector.parse_remote_snapshot("HOST", line)
    assert result["panes"][0]["window_created"] == ""
    assert result["panes"][0]["pane_id"] == "%1"


def test_parse_snapshot_malformed_line_is_skipped():
    result = collector.parse_remote_snapshot("HOST", "not-enough-fields")
    assert result["panes"] == []
    assert result["status"] == collector.HOST_STATUS_ONLINE


def test_fetch_remote_timeout_degrades_gracefully(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

    monkeypatch.setattr(collector, "_run_ssh", fake_run)
    result = collector.fetch_remote("nonexistent-host", timeout=0.1)
    assert result["status"] == collector.HOST_STATUS_OFFLINE
    assert result["panes"] == []


def test_fetch_remote_nonzero_exit_is_offline(monkeypatch):
    class FakeResult:
        returncode = 255
        stdout = ""

    monkeypatch.setattr(collector, "_run_ssh", lambda *a, **k: FakeResult())
    result = collector.fetch_remote("nonexistent-host")
    assert result["status"] == collector.HOST_STATUS_OFFLINE


def test_fetch_remote_generic_exception_is_offline(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(collector, "_run_ssh", fake_run)
    result = collector.fetch_remote("nonexistent-host")
    assert result["status"] == collector.HOST_STATUS_OFFLINE


def test_remote_result_history_requires_exact_live_pane_identity(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = "› prompt\ncomplete answer\nWorked for 2s\n› Ask Codex to do anything\n"

    monkeypatch.setattr(collector, "_run_ssh", lambda host, script, timeout: captured.update(
        host=host, script=script, timeout=timeout
    ) or FakeResult())

    lines = collector.fetch_remote_history("registered-host", "%12", "345", lines=800, session_id="$12")

    assert lines == ["› prompt", "complete answer", "Worked for 2s", "› Ask Codex to do anything"]
    assert captured["host"] == "registered-host"
    assert "#{pane_pid}" in captured["script"] and "= 345" in captured["script"]
    assert "#{pane_dead}" in captured["script"] and "= 0" in captured["script"]
    assert "#{session_id}" in captured["script"] and "= '$12'" in captured["script"]
    assert "capture-pane" in captured["script"] and "-S -800" in captured["script"]


@pytest.mark.parametrize(
    ("host", "pane_id", "pane_pid", "lines"),
    [("-oProxyCommand=x", "%1", "2", 800), ("host", "%x", "2", 800), ("host", "%1", "x", 800), ("host", "%1", "2", 0)],
)
def test_remote_result_history_rejects_untrusted_target_parts(host, pane_id, pane_pid, lines):
    assert collector.fetch_remote_history(host, pane_id, pane_pid, lines=lines) is None


@pytest.mark.parametrize("session_id", ["main; echo bad", "$x", "-1"])
def test_remote_result_history_rejects_untrusted_session_id(session_id):
    assert collector.fetch_remote_history("host", "%1", "2", session_id=session_id) is None
