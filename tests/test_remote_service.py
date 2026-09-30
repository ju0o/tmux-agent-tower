"""In-TUI Remote control: shared service lifecycle, menu text, config."""

import time

import pytest

from tmux_agent_tower.i18n import en, ko
from tmux_agent_tower.launcher import config
from tmux_agent_tower.server import auth, service
from tmux_agent_tower.ui import remote_menu


class _Proc:
    def __init__(self, pid=4242, exit_code=None):
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit


def _use_config(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(service, "_status_cache", {"at": 0.0, "value": None})


# -- status -----------------------------------------------------------------


def test_status_stopped_when_nothing_recorded(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    assert service.status(force=True).state == "stopped"


def test_status_running_checks_health_and_tailscale(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {
        "pid": 7, "port": 4312, "mode": "tailscale", "url": "https://box.ts.net/", "https": True, "ready": True,
    })
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "process_is_ours", lambda pid: True)
    monkeypatch.setattr(service, "_health_ok", lambda port: True)
    monkeypatch.setattr(service, "_tailscale_mapping_ok", lambda port: True)

    found = service.status(force=True)
    assert found.state == "running"
    assert found.url == "https://box.ts.net/"
    assert found.https is True


def test_status_error_when_health_fails(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {"pid": 7, "port": 4312, "mode": "tailscale", "url": "https://x/"})
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "process_is_ours", lambda pid: True)
    monkeypatch.setattr(service, "_health_ok", lambda port: False)

    assert service.status(force=True).state == "error"
    assert service.status(force=True).reason == "health_failed"


def test_status_error_when_tailscale_mapping_is_gone(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {"pid": 7, "port": 4312, "mode": "tailscale", "url": "https://x/"})
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "process_is_ours", lambda pid: True)
    monkeypatch.setattr(service, "_health_ok", lambda port: True)
    monkeypatch.setattr(service, "_tailscale_mapping_ok", lambda port: False)

    found = service.status(force=True)
    assert found.state == "error"
    assert found.reason == "serve_mapping_missing"


def test_stale_pid_is_forgotten_and_not_killed(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {"pid": 99999, "port": 4312, "mode": "tailscale"})
    kills = []
    monkeypatch.setattr(service, "pid_alive", lambda pid: False)
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    found = service.status(force=True)
    assert found.state == "stopped"
    assert found.stale is True
    assert not service._runtime_path().exists()
    assert kills == []


def test_reused_pid_is_not_ours_and_not_killed(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {"pid": 4, "port": 4312, "mode": "local"})
    kills = []
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "process_is_ours", lambda pid: False)
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: kills.append(sig))

    assert service.status(force=True).state == "stopped"
    assert kills == []


def test_stop_refuses_a_process_that_is_not_ours(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {"pid": 4, "port": 4312, "mode": "tailscale"})
    kills = []
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "process_is_ours", lambda pid: False)
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: kills.append(sig))

    assert service.stop() == "not_ours"
    assert kills == []


def test_stop_signals_only_our_pid(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {"pid": 4, "port": 4312, "mode": "local"})
    kills = []
    alive = {"value": True}
    monkeypatch.setattr(service, "pid_alive", lambda pid: alive["value"])
    monkeypatch.setattr(service, "process_is_ours", lambda pid: True)

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        alive["value"] = False

    monkeypatch.setattr(service.os, "kill", fake_kill)
    assert service.stop() == "stopped"
    assert kills == [(4, service.signal.SIGTERM)]


# -- start ------------------------------------------------------------------


def test_duplicate_start_does_not_spawn(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service,
        "status",
        lambda force=False: service.ServiceStatus(state="running", url="https://box.ts.net/", https=True, mode="tailscale"),
    )
    monkeypatch.setattr(service, "pairing_info", lambda: {"code": "123456", "expired": False})
    monkeypatch.setattr(service, "_spawn", lambda *a, **k: pytest.fail("must not spawn a second server"))

    result = service.start("tailscale")
    assert result.ok is True
    assert result.already_running is True
    assert result.url == "https://box.ts.net/"
    assert result.pairing_code == "123456"


def test_start_reports_tailscale_unavailable_without_raising(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="stopped"))

    def fake_spawn(mode, port):
        service._write_last_error("tailscale_missing")
        return _Proc(exit_code=1)

    monkeypatch.setattr(service, "_spawn", fake_spawn)
    result = service.start("tailscale")
    assert result.ok is False
    assert result.reason == "tailscale_missing"


def test_spawn_uses_an_argument_list_not_a_shell(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    service._spawn("tailscale", 4312)

    assert isinstance(captured["cmd"], list)
    assert "tmux_agent_tower.server.service" in captured["cmd"]
    assert "--mode" in captured["cmd"] and "tailscale" in captured["cmd"]
    assert captured["kwargs"].get("shell") in (None, False)
    assert captured["kwargs"]["stdin"] is service.subprocess.DEVNULL


# -- pairing ----------------------------------------------------------------


def test_pairing_info_hides_an_expired_code(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {
        "url": "https://box.ts.net/",
        "pairing_code": "123456",
        "pairing_expires_at": time.time() - 10,
        "https": True,
    })
    info = service.pairing_info()
    assert info["expired"] is True
    assert info["code"] is None
    assert info["url"] == "https://box.ts.net/"


def test_regenerate_pairing_is_a_local_control_file(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), {
        "pid": 1,
        "url": "https://box.ts.net/",
        "pairing_code": "111111",
        "pairing_expires_at": time.time() + 100,
    })
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="running"))

    def update_on_sleep(_seconds):
        service._write_json(service._runtime_path(), {
            "pid": 1,
            "url": "https://box.ts.net/",
            "pairing_code": "222222",
            "pairing_expires_at": time.time() + 100,
        })

    monkeypatch.setattr(service.time, "sleep", update_on_sleep)
    info = service.regenerate_pairing()
    assert info["code"] == "222222"
    # The TUI only drops a control file. The server process is what
    # consumes it; this test has no server, so the request stays on disk.
    assert (tmp_path / "remote-control" / "regenerate").is_file()


def test_regenerate_does_nothing_when_stopped(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="stopped"))
    assert service.regenerate_pairing() is None
    assert not (tmp_path / "remote-control").exists()


# -- autostart --------------------------------------------------------------


def test_autostart_off_does_not_start(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"remote_autostart": False})
    monkeypatch.setattr(service, "start", lambda *a, **k: pytest.fail("must not start"))
    assert service.maybe_autostart() is None


def test_autostart_on_starts_tailscale(monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: {"remote_autostart": True})
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="stopped"))
    calls = []
    monkeypatch.setattr(service, "start", lambda mode="tailscale": calls.append(mode) or service.StartResult(ok=True))
    result = service.maybe_autostart()
    assert calls == ["tailscale"]
    assert result.ok is True


def test_autostart_failure_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: {"remote_autostart": True})
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="stopped"))

    def boom(*args, **kwargs):
        raise RuntimeError("tailscale down")

    monkeypatch.setattr(service, "start", boom)
    result = service.maybe_autostart()
    assert result is not None and result.ok is False
    assert result.reason == "autostart_failed"


def test_tui_exit_does_not_stop_remote(monkeypatch):
    monkeypatch.setattr(service, "stop", lambda: pytest.fail("quit must not stop remote"))
    service.on_tui_exit()


# -- config -----------------------------------------------------------------


def test_remote_autostart_persists_without_clobbering_other_keys(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('language = "ko"\nproject_roots = ["~/Projects"]\n', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)

    config.set_remote_flag("autostart", True)
    cfg = config.load_config()
    assert cfg["remote_autostart"] is True
    assert cfg["remote_intro_seen"] is False

    config.set_remote_flag("intro_seen", True)
    text = config_file.read_text(encoding="utf-8")
    assert 'language = "ko"' in text
    assert 'project_roots = ["~/Projects"]' in text
    assert text.count("[remote]") == 1
    assert config.load_config()["remote_intro_seen"] is True

    config.set_remote_flag("autostart", False)
    assert config.load_config()["remote_autostart"] is False
    assert 'language = "ko"' in config_file.read_text(encoding="utf-8")


# -- menu render ------------------------------------------------------------


def test_remote_menu_entries_match_the_spec():
    keys = [key for key, _label in remote_menu.menu_entries()]
    assert keys == ["start", "info", "devices", "autostart", "stop", "back"]


def test_badges_for_each_state():
    assert "○" in remote_menu.badge_text("stopped")
    assert "●" in remote_menu.badge_text("running")
    assert "!" in remote_menu.badge_text("error")


def test_ready_screen_shows_url_and_code_but_no_shell_command():
    lines = remote_menu.ready_lines(service.StartResult(
        ok=True, url="https://box.ts.net/", pairing_code="482193", https=True, tailscale_ok=True,
    ))
    text = "\n".join(lines)
    assert "https://box.ts.net/" in text
    assert "482 193" in text
    assert "tower serve" not in text
    assert "venv" not in text


def test_failure_screen_for_missing_tailscale_has_no_command():
    text = "\n".join(remote_menu.failure_lines("tailscale_missing"))
    assert "tower serve" not in text
    assert text.strip()


def test_autostart_menu_marks_the_active_choice():
    on_labels = dict(remote_menu.autostart_entries(True))
    off_labels = dict(remote_menu.autostart_entries(False))
    assert on_labels["on"].startswith("[✓]")
    assert off_labels["off"].startswith("[✓]")
    assert off_labels["on"].startswith("[ ]")


def test_remote_strings_exist_in_korean_and_english():
    ko_keys = {key for key in ko.STRINGS if key.startswith("remote.badge") or key.startswith("remote.menu") or key.startswith("remote.error")}
    en_keys = {key for key in en.STRINGS if key.startswith("remote.badge") or key.startswith("remote.menu") or key.startswith("remote.error")}
    assert ko_keys == en_keys
    assert "remote.menu.start" in ko_keys
    assert ko.STRINGS["hint.remote"].startswith("M ")
    assert en.STRINGS["hint.remote"].startswith("M ")


# -- token metadata, backward compatible -----------------------------------


def test_device_list_reads_old_tokens_and_hides_the_secret(tmp_path):
    store = auth.TokenStore(tmp_path / "tokens.json")
    store.add_token("legacy-token")
    store.add_token("labeled-token", label="Galaxy S20")
    devices = store.list_devices()
    dumped = str(devices)
    assert "legacy-token" not in dumped
    assert "labeled-token" not in dumped
    labels = {device["label"] for device in devices}
    assert "Galaxy S20" in labels
    assert "" in labels  # old records simply have no label
    galaxy = next(device for device in devices if device["label"] == "Galaxy S20")
    assert store.revoke_id(galaxy["id"]) is True
    assert store.is_valid("labeled-token") is False
    assert store.is_valid("legacy-token") is True
