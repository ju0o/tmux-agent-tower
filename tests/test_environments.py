from tmux_agent_tower.state.environments import (
    add_profile,
    load_profiles,
    remove_profile,
    rename_profile,
    ssh_target_available,
)


def test_legacy_remote_hosts_migrate_in_memory_to_generic_ssh_profiles(tmp_path):
    path = tmp_path / "remote-hosts.txt"
    path.write_text("# existing\nremote-host:원격 개발 서버\nlocal-machine\n", encoding="utf-8")

    profiles = load_profiles(path)

    assert profiles == [
        {"alias": "remote-host", "name": "원격 개발 서버", "display_name": "원격 개발 서버", "transport": "ssh", "ssh_target": "remote-host"},
        {"alias": "local-machine", "name": "local-machine", "display_name": "local-machine", "transport": "ssh", "ssh_target": "local-machine"},
    ]
    assert path.read_text(encoding="utf-8") == "# existing\nremote-host:원격 개발 서버\nlocal-machine\n"


def test_add_rename_remove_profile_only_changes_tower_profile(tmp_path):
    path = tmp_path / "remote-hosts.txt"
    path.write_text("# keep me\nold:Old name\n", encoding="utf-8")

    add_profile(path, "원격 개발 서버", "remote-host")
    rename_profile(path, "remote-host", "개발 노트북")
    remove_profile(path, "remote-host")

    assert load_profiles(path) == [{
        "alias": "old", "name": "Old name", "display_name": "Old name",
        "transport": "ssh", "ssh_target": "old",
    }]
    assert path.read_text(encoding="utf-8") == "# keep me\nold:Old name\n"


def test_profile_validation_rejects_duplicate_targets_and_ssh_option_injection(tmp_path):
    path = tmp_path / "remote-hosts.txt"
    add_profile(path, "Laptop", "remote-host")
    for target in ("remote-host", "-oProxyCommand=bad", "bad host", "bad\nother"):
        try:
            add_profile(path, "Another", target)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe or duplicate SSH target accepted: {target!r}")


def test_ssh_target_accepts_user_at_host():
    from tmux_agent_tower.state.environments import valid_ssh_target

    assert valid_ssh_target("dev@example.internal")
    assert not valid_ssh_target("-oProxyCommand=bad")


def test_connection_test_uses_batch_ssh_and_fails_closed(monkeypatch):
    seen = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        "tmux_agent_tower.state.environments.subprocess.run",
        lambda argv, **kwargs: seen.append((argv, kwargs)) or Result(),
    )

    assert ssh_target_available("remote-host") is True
    assert seen[0][0][-2:] == ["remote-host", "true"]
    assert "BatchMode=yes" in seen[0][0]
    assert ssh_target_available("-oProxyCommand=bad") is False
