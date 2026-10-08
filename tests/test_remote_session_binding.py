"""Tower Remote is bound to an explicit tmux session.

Regression coverage for the phone showing only the SSH host and "no
panes": the detached child used to re-infer its session from its own
environment (a scratch session that was later deleted) and stayed
"healthy" while serving an empty local list.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from tmux_agent_tower import main
from tmux_agent_tower.i18n import en, ko
from tmux_agent_tower.server import httpapi, service
from tmux_agent_tower.tmux import discovery
from tmux_agent_tower.ui import remote_menu
from tmux_agent_tower.ui import tower as tower_module


class _Proc:
    def __init__(self, pid=4242, exit_code=None):
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit


class _RecordingServer:
    def __init__(self):
        self.allowed_hosts = None
        self.shutdown_calls = 0
        self.closed = False

        class _Pairing:
            def current_code(self):
                return "000000"

            def snapshot(self):
                return {"code": "000000", "expires_at": None, "expired": False}

        self.pairing = _Pairing()

    def serve_forever(self):
        return

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.closed = True


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(service, "_status_cache", {"at": 0.0, "value": None})


def _quiet_foreground(monkeypatch):
    monkeypatch.setattr(service.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(service.tailscale, "stop_serve", lambda exe, port: True)


# -- detached child: explicit session, no inference --------------------------


def test_spawn_passes_session_and_own_pane_to_the_child(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    service._spawn("tailscale", 4312, "scratch-A", "%7")

    cmd = captured["cmd"]
    assert cmd[cmd.index("--tmux-session") + 1] == "scratch-A"
    assert cmd[cmd.index("--own-pane-id") + 1] == "%7"


def test_child_entrypoint_forwards_session_and_never_infers_it(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        service.tmux_capture, "current_session", lambda: pytest.fail("child must not infer its session")
    )
    monkeypatch.setattr(
        service.tmux_capture, "current_pane_id", lambda: pytest.fail("child must not infer its pane")
    )

    def fake_serve(mode, port, detached=False, session="", own_pane_id=""):
        calls.update(mode=mode, port=port, detached=detached, session=session, own_pane_id=own_pane_id)
        return 0

    monkeypatch.setattr(service, "serve_foreground", fake_serve)
    code = service._main(
        ["--foreground", "--detached", "--mode", "tailscale", "--port", "4312", "--tmux-session", "main", "--own-pane-id", "%3"]
    )
    assert code == 0
    assert calls == {"mode": "tailscale", "port": 4312, "detached": True, "session": "main", "own_pane_id": "%3"}


def test_child_entrypoint_refuses_to_start_without_a_session(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "serve_foreground", lambda *a, **k: pytest.fail("must not serve"))
    assert service._main(["--foreground", "--detached", "--mode", "tailscale"]) == 2
    assert json.loads((tmp_path / "remote-last-error.json").read_text())["reason"] == "no_tmux"


def test_serve_foreground_uses_the_given_session_and_pane(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _quiet_foreground(monkeypatch)
    monkeypatch.setattr(service.tmux_capture, "current_session", lambda: pytest.fail("must not infer"))
    monkeypatch.setattr(service.tmux_capture, "current_pane_id", lambda: pytest.fail("must not infer"))
    monkeypatch.setattr(service.tmux_capture, "session_exists", lambda session: session == "scratch-A")
    created = []

    def fake_create_server(host, port, session, own_pane_id, allowed_hosts, token_path=None):
        created.append((session, own_pane_id))
        return _RecordingServer()

    monkeypatch.setattr(service.httpapi, "create_server", fake_create_server)

    assert service.serve_foreground("local", 4312, detached=True, session="scratch-A", own_pane_id="%7") == 0
    assert created == [("scratch-A", "%7")]


def test_serve_foreground_refuses_a_session_that_does_not_exist(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _quiet_foreground(monkeypatch)
    monkeypatch.setattr(service.tmux_capture, "session_exists", lambda session: False)
    monkeypatch.setattr(service.httpapi, "create_server", lambda *a, **k: pytest.fail("must not bind"))

    assert service.serve_foreground("local", 4312, detached=True, session="gone") == 1
    assert json.loads((tmp_path / "remote-last-error.json").read_text())["reason"] == "source_session_missing"


def test_runtime_file_records_the_target_session(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    service._publish(_RecordingServer(), "tailscale", 4312, "https://tower.example.invalid/", True, "main", "%3")
    runtime = json.loads((tmp_path / "remote-runtime.json").read_text())
    assert runtime["tmux_session"] == "main"
    assert runtime["own_pane_id"] == "%3"


# -- CLI: session resolved in the foreground process --------------------------


def test_cli_serve_resolves_session_before_handing_off(monkeypatch):
    monkeypatch.setattr(main.tmux_capture, "current_session", lambda: "cli-sess")
    monkeypatch.setattr(main.tmux_capture, "current_pane_id", lambda: "%9")
    calls = {}

    def fake_serve(mode, port, detached=False, session="", own_pane_id=""):
        calls.update(mode=mode, port=port, detached=detached, session=session, own_pane_id=own_pane_id)
        return 0

    monkeypatch.setattr(main.remote_service, "serve_foreground", fake_serve)
    main.run_serve(False, 4312, use_tailscale=True)
    assert calls == {"mode": "tailscale", "port": 4312, "detached": False, "session": "cli-sess", "own_pane_id": "%9"}


def test_cli_serve_outside_tmux_exits_without_starting(monkeypatch, capsys):
    monkeypatch.setattr(main.tmux_capture, "current_session", lambda: "")
    monkeypatch.setattr(main.remote_service, "serve_foreground", lambda *a, **k: pytest.fail("must not serve"))
    with pytest.raises(SystemExit) as exc:
        main.run_serve(False, 4312)
    assert exc.value.code == 1
    assert capsys.readouterr().err.strip()


# -- status: session liveness -------------------------------------------------


def _running_runtime(session="main"):
    return {
        "pid": 7, "port": 4312, "mode": "tailscale", "url": "https://tower.example.invalid/", "https": True, "ready": True,
        "tmux_session": session, "own_pane_id": "%1",
    }


def _healthy(monkeypatch):
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "process_is_ours", lambda pid: True)
    monkeypatch.setattr(service, "_health_ok", lambda port: True)
    monkeypatch.setattr(service, "_tailscale_mapping_ok", lambda port: True)


def test_status_is_running_only_while_the_source_session_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _healthy(monkeypatch)
    service._write_json(service._runtime_path(), _running_runtime("main"))
    monkeypatch.setattr(service.tmux_capture, "session_exists", lambda session: session == "main")

    found = service.status(force=True)
    assert found.state == "running"
    assert found.session == "main"


def test_deleted_source_session_is_an_error_even_with_backend_and_serve_up(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _healthy(monkeypatch)
    service._write_json(service._runtime_path(), _running_runtime("scratch-A"))
    monkeypatch.setattr(service.tmux_capture, "session_exists", lambda session: False)

    found = service.status(force=True)
    assert found.state == "error"
    assert found.reason == "source_session_missing"
    assert found.session == "scratch-A"
    assert remote_menu.badge_text(found.state) == remote_menu.badge_text("error")


def test_legacy_runtime_without_a_session_is_not_reported_running(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _healthy(monkeypatch)
    runtime = _running_runtime()
    del runtime["tmux_session"]
    service._write_json(service._runtime_path(), runtime)
    monkeypatch.setattr(service.tmux_capture, "session_exists", lambda session: pytest.fail("empty session must short-circuit"))

    found = service.status(force=True)
    assert found.state == "error"
    assert found.reason == "source_session_missing"


def test_pairing_info_includes_the_session(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    service._write_json(service._runtime_path(), _running_runtime("main"))
    assert service.pairing_info()["session"] == "main"


# -- start: same session no-op, other session detected, rebind ---------------


def test_start_without_a_session_is_refused(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "_spawn", lambda *a, **k: pytest.fail("must not spawn"))
    result = service.start("tailscale", session="")
    assert result.ok is False
    assert result.reason == "no_tmux"


def test_duplicate_start_for_the_same_session_is_a_noop(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service, "status",
        lambda force=False: service.ServiceStatus(state="running", pid=7, url="https://tower.example.invalid/", session="main", mode="tailscale"),
    )
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "pairing_info", lambda: {"code": "123456", "expired": False})
    monkeypatch.setattr(service, "_spawn", lambda *a, **k: pytest.fail("must not spawn"))

    result = service.start("tailscale", session="main")
    assert result.ok is True and result.already_running is True
    assert result.session == "main"


def test_start_detects_a_remote_bound_to_another_session(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service, "status",
        lambda force=False: service.ServiceStatus(state="running", pid=7, url="https://tower.example.invalid/", session="scratch-A", mode="tailscale"),
    )
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "pairing_info", lambda: {"code": "123456", "expired": False})
    stops = []
    monkeypatch.setattr(service, "stop", lambda: stops.append(1) or "stopped")
    monkeypatch.setattr(service, "_spawn", lambda *a, **k: pytest.fail("must not spawn or switch on its own"))

    result = service.start("tailscale", session="main")
    assert result.ok is False
    assert result.reason == "session_mismatch"
    assert result.running_session == "scratch-A"
    assert result.session == "main"
    assert stops == []


def test_start_detects_mismatch_when_the_old_session_is_already_gone(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service, "status",
        lambda force=False: service.ServiceStatus(
            state="error", reason="source_session_missing", pid=7, session="scratch-A", mode="tailscale"
        ),
    )
    monkeypatch.setattr(service, "pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "pairing_info", lambda: {"code": None, "expired": True})
    monkeypatch.setattr(service, "_spawn", lambda *a, **k: pytest.fail("must not spawn"))

    result = service.start("tailscale", session="main")
    assert result.reason == "session_mismatch"
    assert result.running_session == "scratch-A"


def test_rebind_stops_ours_then_starts_for_the_current_tower(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(service, "stop", lambda: order.append("stop") or "stopped")

    def fake_start(mode, port=service.DEFAULT_PORT, session="", own_pane_id=""):
        order.append(("start", mode, session, own_pane_id))
        return service.StartResult(ok=True, session=session)

    monkeypatch.setattr(service, "start", fake_start)
    result = service.rebind("tailscale", session="main", own_pane_id="%3")
    assert order == ["stop", ("start", "tailscale", "main", "%3")]
    assert result.ok is True and result.session == "main"


def test_rebind_never_kills_a_process_that_is_not_ours(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "stop", lambda: "not_ours")
    monkeypatch.setattr(service, "start", lambda *a, **k: pytest.fail("must not start over a foreign process"))
    result = service.rebind("tailscale", session="main")
    assert result.ok is False and result.reason == "not_ours"


# -- autostart binds to the Tower's session ----------------------------------


def test_autostart_passes_the_tower_session(monkeypatch, tmp_path):
    from tmux_agent_tower.launcher import config

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: {"remote_autostart": True})
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="stopped"))
    calls = []

    def fake_start(mode, port=service.DEFAULT_PORT, session="", own_pane_id=""):
        calls.append((session, own_pane_id))
        return service.StartResult(ok=True, session=session)

    monkeypatch.setattr(service, "start", fake_start)
    service.maybe_autostart(session="main", own_pane_id="%3")
    assert calls == [("main", "%3")]


def test_autostart_without_a_session_starts_nothing(monkeypatch, tmp_path):
    from tmux_agent_tower.launcher import config

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: {"remote_autostart": True})
    monkeypatch.setattr(service, "start", lambda *a, **k: pytest.fail("autostart must not guess a session"))
    assert service.maybe_autostart(session="") is None


def test_autostart_skips_when_a_remote_already_watches_this_session(monkeypatch, tmp_path):
    from tmux_agent_tower.launcher import config

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "load_config", lambda: {"remote_autostart": True})
    monkeypatch.setattr(service, "status", lambda force=False: service.ServiceStatus(state="running", pid=7, session="main"))
    monkeypatch.setattr(service, "start", lambda *a, **k: pytest.fail("must not start twice"))
    assert service.maybe_autostart(session="main") is None


# -- HTTP API: never a silent empty list -------------------------------------


@pytest.fixture
def api_server(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "host").write_text("local-fixture\n", encoding="utf-8")
    (tmp_path / "config" / "remote-hosts.txt").write_text("remote-fixture.example:remote-fixture\n", encoding="utf-8")

    # The SSH host answers fine; the local session is what disappears.
    monkeypatch.setattr(
        tower_module, "fetch_remote",
        lambda alias, display_name=None, timeout=0: {
            "host": display_name, "status": tower_module.HOST_STATUS_ONLINE,
            "panes": [{"pane_id": "%1", "title": "remote-fixture-title", "command": "bash", "path": "/x", "dead": False, "lines": []}],
        },
    )
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "")
    alive = {"value": True}
    monkeypatch.setattr(httpapi.tmux_capture, "session_exists", lambda session: alive["value"] and session == "main")

    srv = httpapi.create_server("127.0.0.1", 0, "main", "", ["localhost", "127.0.0.1"], token_path=tmp_path / "tokens.json")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, alive
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(srv, path, token):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _pair(srv):
    data = json.dumps({"code": srv.pairing.current_code()}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/pair", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read())["token"]


def test_status_names_the_session_while_it_exists(api_server):
    srv, _alive = api_server
    token = _pair(srv)
    status, body = _get(srv, "/api/status", token)
    assert status == 200
    assert body["ok"] is True
    assert body["session"] == "main"
    assert any(p["host"] == "remote-fixture" for p in body["panes"])


def test_deleted_source_session_is_an_explicit_error_not_an_empty_list(api_server):
    srv, alive = api_server
    token = _pair(srv)
    alive["value"] = False

    status, body = _get(srv, "/api/status", token)
    assert status == 503
    assert body == {"ok": False, "error": "source_session_missing", "session": "main"}
    # No pane list at all: the SSH host must not appear alone as if the
    # local Tower simply had nothing to show.
    assert "panes" not in body


def test_prompt_is_refused_once_the_source_session_is_gone(api_server):
    srv, alive = api_server
    token = _pair(srv)
    alive["value"] = False
    data = json.dumps({"pane_key": "%1", "text": "hi"}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/prompt", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 409
    assert json.loads(exc.value.read())["error"] == "source_session_missing"


def test_phone_page_explains_the_ended_session():
    from tmux_agent_tower.server.webui import PAGE_HTML

    assert "source_session_missing" in PAGE_HTML
    assert "연결하던 작업 화면이 끝났어요." in PAGE_HTML


# -- menu text ---------------------------------------------------------------


def test_menu_preamble_shows_current_session_and_a_mismatched_remote():
    running_elsewhere = service.ServiceStatus(state="running", pid=7, session="scratch-A")
    lines = remote_menu.session_lines("main", running_elsewhere)
    assert any("main" in line for line in lines)
    assert any("scratch-A" in line for line in lines)

    same = service.ServiceStatus(state="running", pid=7, session="main")
    assert not any("scratch" in line for line in remote_menu.session_lines("main", same))


def test_menu_preamble_explains_a_missing_source_session():
    gone = service.ServiceStatus(state="error", reason="source_session_missing", pid=7, session="scratch-A")
    text = "\n".join(remote_menu.session_lines("main", gone))
    assert "scratch-A" in text
    assert ko.STRINGS["remote.error.source_session_missing"] in text or en.STRINGS["remote.error.source_session_missing"] in text


def test_ready_screen_names_the_session_it_watches():
    lines = remote_menu.ready_lines(service.StartResult(ok=True, url="https://tower.example.invalid/", pairing_code="482193", session="main"))
    assert any("main" in line for line in lines)


def test_rebind_menu_offers_switch_keep_cancel_in_both_languages():
    keys = [key for key, _ in remote_menu.rebind_entries()]
    assert keys == ["switch", "keep", "cancel"]
    for catalog in (ko.STRINGS, en.STRINGS):
        for key in ("remote.rebind.title", "remote.rebind.switch", "remote.rebind.keep", "remote.rebind.cancel",
                    "remote.session.label", "remote.session.current", "remote.session.remote",
                    "remote.error.source_session_missing", "remote.error.session_mismatch"):
            assert key in catalog
