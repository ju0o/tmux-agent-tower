"""``tower serve --tailscale`` wiring in ``main.py`` -- everything that
``server/tailscale.py``'s unit tests can't see: bind address, Host
allowlist additions, fail-closed ordering, and shutdown cleanup.

All Tailscale / curl.exe calls are mocked; nothing here shells out.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from tmux_agent_tower import main
from tmux_agent_tower.server import httpapi, tailscale

DNS_NAME = "mybox.tailxxxxx.ts.net"
TS_IP = "100.64.1.2"
FAKE_STATUS = {"Self": {"DNSName": DNS_NAME + ".", "TailscaleIPs": [TS_IP, "fd7a:115c:a1e0::1"]}}


class _FakeServer:
    def __init__(self):
        self.allowed_hosts = ["localhost", "127.0.0.1"]


# -- _setup_tailscale: fail-closed ordering ---------------------------------


def test_setup_fails_when_tailscale_missing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: None)
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: calls.append("reach") or True)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: calls.append("serve") or (True, DNS_NAME))

    server = _FakeServer()
    assert main._setup_tailscale(server, 4312) == (None, None)
    assert calls == []  # nothing else attempted once Tailscale is absent
    assert server.allowed_hosts == ["localhost", "127.0.0.1"]
    assert "Tailscale was not found" in capsys.readouterr().err


def test_setup_fails_when_windows_reachability_unverifiable(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: "fake.exe")
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: None)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: calls.append("serve") or (True, DNS_NAME))

    assert main._setup_tailscale(_FakeServer(), 4312) == (None, None)
    assert calls == []  # Serve is never configured on an unverified path
    assert "Windows localhost에서 Tower Remote에 연결할 수 없습니다" in capsys.readouterr().err


def test_setup_fails_when_windows_cannot_reach_backend(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: "fake.exe")
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: False)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: calls.append("serve") or (True, DNS_NAME))

    assert main._setup_tailscale(_FakeServer(), 4312) == (None, None)
    assert calls == []
    assert "Windows localhost에서 Tower Remote에 연결할 수 없습니다." in capsys.readouterr().err


def test_setup_refuses_existing_unrelated_mapping(monkeypatch, capsys):
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: "fake.exe")
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: True)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: (False, "existing_mapping:http://127.0.0.1:9999"))

    server = _FakeServer()
    assert main._setup_tailscale(server, 4312) == (None, None)
    assert server.allowed_hosts == ["localhost", "127.0.0.1"]
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "http://127.0.0.1:9999" in err
    assert "will not overwrite" in err


@pytest.mark.parametrize("reason", ["tailscale_status_unavailable", "no_magicdns", "serve_command_failed"])
def test_setup_reports_each_known_failure_reason(monkeypatch, capsys, reason):
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: "fake.exe")
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: True)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: (False, reason))

    assert main._setup_tailscale(_FakeServer(), 4312) == (None, None)
    assert main._TAILSCALE_ERROR_MESSAGES[reason] in capsys.readouterr().err


# -- _setup_tailscale: success adds exactly the tailnet identities ----------


def test_setup_success_allowlists_magicdns_name_and_tailscale_ip(monkeypatch):
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: "fake.exe")
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: True)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: (True, DNS_NAME))
    monkeypatch.setattr(tailscale, "get_status", lambda exe: FAKE_STATUS)

    server = _FakeServer()
    assert main._setup_tailscale(server, 4312) == ("fake.exe", DNS_NAME)
    assert server.allowed_hosts == ["localhost", "127.0.0.1", TS_IP, DNS_NAME]
    # Never a wildcard / catch-all.
    assert "*" not in server.allowed_hosts
    assert "" not in server.allowed_hosts


def test_setup_success_without_ipv4_still_allowlists_dns_name(monkeypatch):
    monkeypatch.setattr(tailscale, "find_tailscale_exe", lambda: "fake.exe")
    monkeypatch.setattr(tailscale, "windows_can_reach_backend", lambda port: True)
    monkeypatch.setattr(tailscale, "start_serve", lambda exe, port: (True, DNS_NAME))
    monkeypatch.setattr(tailscale, "get_status", lambda exe: {"Self": {"TailscaleIPs": []}})

    server = _FakeServer()
    assert main._setup_tailscale(server, 4312) == ("fake.exe", DNS_NAME)
    assert server.allowed_hosts == ["localhost", "127.0.0.1", DNS_NAME]


# -- run_serve: bind address + lifecycle -------------------------------------


class _RecordingServer:
    """Stands in for the real HTTP server so run_serve's control flow can
    be exercised without opening a socket."""

    def __init__(self):
        self.allowed_hosts = None
        self.shutdown_calls = 0
        self.closed = False

        class _Pairing:
            def current_code(self):
                return "000000"

        self.pairing = _Pairing()

    def serve_forever(self):
        return  # thread exits immediately -> run_serve's wait loop ends

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.closed = True


def _stub_tmux(monkeypatch):
    monkeypatch.setattr(main.tmux_capture, "current_session", lambda: "sess")
    monkeypatch.setattr(main.tmux_capture, "current_pane_id", lambda: "%1")
    monkeypatch.setattr(main.threading.Thread, "start", lambda self: None)  # no stdin watcher, no serve thread


def _capture_create_server(monkeypatch, created):
    def fake_create_server(host, port, session, own_pane_id, allowed_hosts):
        srv = _RecordingServer()
        srv.allowed_hosts = allowed_hosts
        created.append((host, port, srv))
        return srv

    monkeypatch.setattr(main.httpapi, "create_server", fake_create_server)


def test_tailscale_mode_binds_localhost_only(monkeypatch):
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: ("fake.exe", DNS_NAME))
    monkeypatch.setattr(main.tailscale, "stop_serve", lambda exe, port: True)

    main.run_serve(False, 4312, use_tailscale=True)

    assert created[0][0] == "127.0.0.1"
    assert created[0][0] != "0.0.0.0"


def test_tailscale_mode_never_binds_all_interfaces_even_if_lan_is_set(monkeypatch):
    # argparse makes --lan/--tailscale mutually exclusive; this guards the
    # function-level contract in case run_serve is ever called directly.
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: ("fake.exe", DNS_NAME))
    monkeypatch.setattr(main.tailscale, "stop_serve", lambda exe, port: True)
    monkeypatch.setattr(main.netutil, "detect_lan_ip", lambda: pytest.fail("LAN IP must not be detected in tailscale mode"))

    main.run_serve(True, 4312, use_tailscale=True)

    assert created[0][0] == "127.0.0.1"


def test_tailscale_setup_failure_exits_and_closes_server(monkeypatch):
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: (None, None))
    stop_calls = []
    monkeypatch.setattr(main.tailscale, "stop_serve", lambda exe, port: stop_calls.append((exe, port)) or True)

    with pytest.raises(SystemExit) as exc:
        main.run_serve(False, 4312, use_tailscale=True)

    assert exc.value.code == 1
    srv = created[0][2]
    assert srv.shutdown_calls == 1
    assert srv.closed is True
    assert stop_calls == []  # nothing was set up, so nothing to tear down


def test_tailscale_mode_prints_https_url_and_pairing_code(monkeypatch, capsys):
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: ("fake.exe", DNS_NAME))
    monkeypatch.setattr(main.tailscale, "stop_serve", lambda exe, port: True)

    main.run_serve(False, 4312, use_tailscale=True)

    out = capsys.readouterr().out
    assert "Tailscale: ONLINE" in out
    assert f"https://{DNS_NAME}/" in out
    assert "Pairing code: 000000" in out
    assert "http://127.0.0.1:4312" not in out  # not the user-facing URL in this mode


def test_shutdown_removes_only_tower_mapping(monkeypatch, capsys):
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: ("fake.exe", DNS_NAME))
    stop_calls = []
    monkeypatch.setattr(main.tailscale, "stop_serve", lambda exe, port: stop_calls.append((exe, port)) or True)

    main.run_serve(False, 4312, use_tailscale=True)

    assert stop_calls == [("fake.exe", 4312)]
    assert "Tailscale Serve mapping removed." in capsys.readouterr().out
    assert created[0][2].closed is True


def test_shutdown_tells_user_when_mapping_could_not_be_removed(monkeypatch, capsys):
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: ("fake.exe", DNS_NAME))
    monkeypatch.setattr(main.tailscale, "stop_serve", lambda exe, port: False)

    main.run_serve(False, 4312, use_tailscale=True)

    err = capsys.readouterr().err
    assert "left in place" in err
    assert "tailscale serve --https=443 off" in err
    assert "reset" not in err  # never suggest the destructive command


def test_lan_mode_unchanged_by_tailscale_wiring(monkeypatch):
    created = []
    _stub_tmux(monkeypatch)
    _capture_create_server(monkeypatch, created)
    monkeypatch.setattr(main.netutil, "detect_lan_ip", lambda: "10.0.0.5")
    setup_calls = []
    monkeypatch.setattr(main, "_setup_tailscale", lambda server, port: setup_calls.append(port) or ("x", "y"))

    main.run_serve(True, 4312, use_tailscale=False)

    assert created[0][0] == "0.0.0.0"
    assert created[0][2].allowed_hosts == ["localhost", "127.0.0.1", "10.0.0.5"]
    assert setup_calls == []


# -- Host header: real server, tailnet identities ---------------------------


@pytest.fixture
def tailnet_server(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")

    srv = httpapi.create_server(
        "127.0.0.1",
        0,
        "sess",
        "",
        allowed_hosts=["localhost", "127.0.0.1", TS_IP, DNS_NAME],
        token_path=tmp_path / "tokens.json",
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(server, path, host_header, token=None):
    port = server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    req.add_header("Host", host_header)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_magicdns_host_header_accepted(tailnet_server):
    status, body = _get(tailnet_server, "/api/health", DNS_NAME)
    assert status == 200
    assert body["ok"] is True


def test_magicdns_host_header_with_port_accepted(tailnet_server):
    status, _ = _get(tailnet_server, "/api/health", f"{DNS_NAME}:443")
    assert status == 200


def test_tailscale_ip_host_header_accepted(tailnet_server):
    status, _ = _get(tailnet_server, "/api/health", f"{TS_IP}:4312")
    assert status == 200


def test_unrelated_ts_net_host_is_rejected(tailnet_server):
    # Another machine's MagicDNS name on the same tailnet is still not us.
    status, body = _get(tailnet_server, "/api/health", "otherbox.tailxxxxx.ts.net")
    assert status == 400
    assert body["error"] == "invalid_host"


def test_pairing_still_required_over_tailnet_host(tailnet_server):
    status, body = _get(tailnet_server, "/api/status", DNS_NAME)
    assert status == 401
    assert body["ok"] is False
