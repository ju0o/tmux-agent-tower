from types import SimpleNamespace

from tmux_agent_tower.access_context import decode_access_context, encode_access_context
from tmux_agent_tower.connect import (
    _valid_target,
    detect_clipboard_provider,
    make_access_context,
    resolve_ssh_target,
    select_tower_session,
)


def test_ssh_config_is_resolved_by_openssh_without_host_guessing():
    seen = []

    def run(argv, **_kwargs):
        seen.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout="hostname work.example\nuser dev\nport 2222\nproxyjump relay\n",
        )

    assert resolve_ssh_target("my-server", run=run) == {
        "hostname": "work.example", "user": "dev", "port": "2222", "proxyjump": "relay",
    }
    assert seen == [["ssh", "-G", "my-server"]]


def test_invalid_ssh_target_is_rejected_before_running_ssh():
    assert not _valid_target("-oProxyCommand=evil")
    assert not _valid_target("two words")
    assert resolve_ssh_target("-oProxyCommand=evil", run=lambda *_a, **_k: None) is None


def test_access_client_is_the_originating_machine_not_the_ssh_target(monkeypatch):
    monkeypatch.setattr("tmux_agent_tower.connect.socket.gethostname", lambda: "origin-device")
    context = make_access_context("tower.example", "wl-copy", now=12.0)

    assert context.access_host == "origin-device"
    assert context.tower_host == "tower.example"
    assert context.transport == "ssh"
    assert context.clipboard_capability == "bridge"
    assert decode_access_context(encode_access_context(context)) == context


def test_clipboard_capability_requires_the_matching_readback_tool():
    tools = {"wl-copy", "wl-paste", "xclip"}
    assert detect_clipboard_provider(
        which=lambda name: f"/usr/bin/{name}" if name in tools else None,
        environ={"WAYLAND_DISPLAY": "wayland-0"}, system="linux",
    ) == "wl-copy"
    assert detect_clipboard_provider(
        which=lambda name: "/usr/bin/wl-copy" if name == "wl-copy" else None,
        environ={"WAYLAND_DISPLAY": "wayland-0"}, system="linux",
    ) == ""


def test_os_provider_capability_selection_uses_existing_platform_rules():
    assert detect_clipboard_provider(
        which=lambda name: f"/usr/bin/{name}" if name == "pbcopy" else None,
        environ={}, system="darwin",
    ) == ""
    assert detect_clipboard_provider(
        which=lambda name: f"/usr/bin/{name}" if name in {"xclip"} else None,
        environ={"DISPLAY": ":0"}, system="linux",
    ) == "xclip"


def test_connect_selects_only_one_registered_live_tower_session():
    assert select_tower_session([]) == ""
    assert select_tower_session(["work"]) == "work"
    assert select_tower_session(["work", "personal"]) == ""
    assert select_tower_session(["work", "personal"], "personal") == "personal"
    assert select_tower_session(["work"], "stale") == ""
