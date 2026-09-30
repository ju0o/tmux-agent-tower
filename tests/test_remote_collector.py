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
    line = "\x1f".join(["sess", "0", "win", "0", "%1", "title", "codex", "/home/x", "0"])
    result = collector.parse_remote_snapshot("HOST", line)
    assert result["status"] == collector.HOST_STATUS_ONLINE
    assert len(result["panes"]) == 1
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
