import os

from tmux_agent_tower.tmux import capture


def test_current_pane_id_reads_tmux_pane_env_var(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%40")
    assert capture.current_pane_id() == "%40"


def test_current_pane_id_empty_when_not_in_tmux(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert capture.current_pane_id() == ""


def test_current_pane_id_does_not_call_tmux(monkeypatch):
    # Regression: must NEVER resolve via `tmux display-message` without a
    # -t target, which reports the *attached client's active pane* rather
    # than the pane this process is actually running in -- those differ
    # whenever something switched the client's view moments earlier (real
    # bug, caught live: a Tower registered the wrong pane_id this way).
    monkeypatch.setenv("TMUX_PANE", "%40")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("current_pane_id() must not shell out to tmux")

    monkeypatch.setattr(capture.subprocess, "run", fail_if_called)
    assert capture.current_pane_id() == "%40"
