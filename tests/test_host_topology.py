"""Execution host is not the same thing as the tmux server that owns the pane."""

import os

from tmux_agent_tower.control.actions import find_prompt_target
from tmux_agent_tower.detection.sshdest import destination_from_argv, find_ssh_client
from tmux_agent_tower.detection.topology import (
    agent_through_ssh,
    classify_transport,
    distinct_physical,
)
from tmux_agent_tower.hostreg import HostRegistry
from tmux_agent_tower.state.bindings import ProjectBindingStore
from tmux_agent_tower.ui.tower import navigator_rows


def _registry():
    return HostRegistry(
        "workstation-a",
        "workstation-a-box",
        [{"alias": "workstation-b", "name": "workstation-b"}, {"alias": "workstation-a", "name": "workstation-a"}],
    )


def _classify(**kwargs):
    base = dict(tmux_host="workstation-a", registry=_registry(), resolver=lambda token: "")
    base.update(kwargs)
    return classify_transport(**base)


def test_local_shell_stays_on_the_tmux_host():
    seen = _classify()
    assert seen["observer_host"] == "workstation-a"
    assert seen["tmux_host"] == "workstation-a"
    assert seen["execution_host"] == "workstation-a"
    assert seen["transport"] == "local"


def test_ssh_alias_moves_execution_and_keeps_tmux_host():
    seen = _classify(ssh_target="workstation-b")
    assert seen["tmux_host"] == "workstation-a"
    assert seen["execution_host"] == "workstation-b"
    assert seen["transport"] == "ssh"
    assert seen["transport_target"] == "workstation-b"
    assert seen["topology_source"] == "process"


def test_user_at_host_uses_the_host_token():
    assert destination_from_argv(["ssh", "user@workstation-b"]) == "workstation-b"
    assert destination_from_argv(["ssh", "-p", "22", "-t", "user@workstation-b", "codex"]) == "workstation-b"
    assert destination_from_argv(["ssh", "-J", "jump", "workstation-b"]) == "workstation-b"
    seen = _classify(ssh_target="workstation-b")
    assert seen["execution_host"] == "workstation-b"


def test_shell_text_that_mentions_ssh_is_not_a_client():
    argv_text = "bash -c 'echo ssh workstation-b'"
    cmdline = {"10": argv_text, "11": "bash"}
    ppid = {"11": "10"}
    assert find_ssh_client("10", cmdline, ppid) == ("", False)


def test_wrapper_child_ssh_is_the_client():
    cmdline = {"10": "bash", "11": "ssh -t workstation-b"}
    ppid = {"11": "10"}
    assert find_ssh_client("10", cmdline, ppid) == ("workstation-b", False)


def test_stale_ssh_does_not_leave_the_tmux_host():
    cmdline = {"10": "ssh <defunct>"}
    ppid = {}
    assert find_ssh_client("10", cmdline, ppid)[0] == ""
    seen = _classify(ssh_stale=True)
    assert seen["execution_host"] == "workstation-a"
    assert seen["transport"] == "unknown"
    assert seen["topology_source"] == "stale"


def test_explicit_binding_wins_over_a_different_process(tmp_path):
    store = ProjectBindingStore(tmp_path / "bindings.json")
    store.record(
        "%43",
        "0",
        "500",
        "/far/project",
        "sample-repo",
        "Codex",
        execution_host="workstation-b",
        transport="ssh",
        transport_target="workstation-b",
        tmux_host="workstation-a",
    )
    bound = store.usable("%43", "0", "500")
    assert bound["project_name"] == "sample-repo"
    seen = _classify(ssh_target="other-box", binding=bound)
    assert seen["execution_host"] == "workstation-b"
    assert seen["topology_source"] == "binding"
    assert seen["tmux_host"] == "workstation-a"


def test_manual_ssh_can_name_the_host_and_leave_the_project_unknown():
    seen = _classify(ssh_target="workstation-b")
    assert seen["execution_host"] == "workstation-b"
    agent, source = agent_through_ssh("Shell", "process", "Codex", ["ask codex"])
    assert (agent, source) == ("Codex", "ui-via-ssh")


def test_direct_tower_on_a_host_uses_that_host():
    registry = HostRegistry("workstation-b", "workstation-b-box", [])
    seen = classify_transport(tmux_host="workstation-b", registry=registry)
    assert seen["observer_host"] == "workstation-b"
    assert seen["tmux_host"] == "workstation-b"
    assert seen["execution_host"] == "workstation-b"
    assert seen["transport"] == "local"


def test_ssh_login_does_not_change_the_observer(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "203.0.113.5 1 203.0.113.9 22")
    monkeypatch.setenv("SSH_CLIENT", "203.0.113.5 1 22")
    seen = _classify()
    assert seen["observer_host"] == "workstation-a"
    assert "SSH_CONNECTION" in os.environ


def test_same_pane_id_on_two_tmux_servers_does_not_collapse():
    local = {
        "tmux_host": "workstation-a",
        "session": "0",
        "pane_id": "%43",
        "remote": False,
        "transport": "ssh",
        "execution_host": "workstation-b",
        "host": "workstation-b",
        "window_id": "@2",
        "window_index": "2",
        "window_name": "work",
        "kind": "pane",
        "project": "(no name)",
        "agent": "Codex",
        "status": "IDLE",
    }
    peer = {
        "tmux_host": "workstation-b",
        "session": "0",
        "pane_id": "%43",
        "remote": True,
        "transport": "local",
        "execution_host": "workstation-b",
        "host": "workstation-b",
        "window_id": "@9",
        "window_index": "1",
        "window_name": "agents",
        "kind": "pane",
        "project": "sample-repo",
        "agent": "Shell",
        "status": "IDLE",
    }
    assert distinct_physical([local, peer, dict(local)]) == [local, peer]
    rows = navigator_rows([local, peer])
    windows = [row for row in rows if row.get("kind") == "window"]
    assert [row["key"] for row in windows] == ["workstation-a:0:@2", "workstation-b:0:@9"]
    assert all("%43" not in row["key"] for row in windows)
    assert rows[0]["host"] == "workstation-b"
    assert {row["pane_id"] for row in rows if row.get("kind") == "pane"} == {"%43"}


def test_prompt_target_stays_the_local_tmux_pane():
    row = {
        "key": "%43",
        "pane_id": "%43",
        "remote": False,
        "host": "workstation-b",
        "execution_host": "workstation-b",
        "tmux_host": "workstation-a",
        "transport": "ssh",
        "project": "sample-repo",
        "agent": "Codex",
        "status": "IDLE",
    }

    class Tower:
        rows = [row]

        def load(self):
            return None

    ok, reason, pane_id = find_prompt_target(Tower(), "%43", "sample-repo", "Codex")
    assert (ok, reason, pane_id) == (True, "", "%43")


def test_resolved_hostname_does_not_create_a_second_host():
    registry = _registry()
    registry.remember("workstation-b", "peer-box.example")
    assert registry.display_for("peer-box.example") == "workstation-b"
    assert registry.display_for("workstation-b") == "workstation-b"
    assert registry.display_for("workstation-a-box") == "workstation-a"
