import json
import urllib.error
import urllib.request

import pytest

from tmux_agent_tower.server import httpapi
from tmux_agent_tower.tmux import discovery
from tmux_agent_tower.ui import tower as tower_module

SESSION = "remote-test-session"


def _pane_line(pane_id="%1", title="my-title", command="bash", path="/home/example/project", dead="0"):
    fs = discovery.FIELD_SEP
    return fs.join([SESSION, "0", "win", "0", pane_id, title, command, path, "123", dead])


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Every test in this file that touches a real ``Tower`` must not read
    or write the actual user's state/config directories -- see
    ``ui/tower.py``'s STATE_DIR/CONFIG_DIR module constants.
    """

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    return tmp_path


@pytest.fixture
def one_pane(monkeypatch, isolated_state):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: _pane_line())
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: ["user@host:~$"])
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "my-project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: "my-project")
    return isolated_state


# -- build_status_payload ------------------------------------------------


def test_status_payload_reflects_a_real_pane(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    payload = httpapi.build_status_payload(tower)

    assert len(payload["panes"]) == 1
    pane = payload["panes"][0]
    assert pane["key"] == "%1"
    assert pane["project"] == "my-project"
    assert "path" not in pane
    assert pane["remote"] is False


def test_status_payload_never_includes_raw_lines(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    payload = httpapi.build_status_payload(tower)
    dumped = json.dumps(payload)
    assert "user@host" not in dumped


# -- find_prompt_target ---------------------------------------------------


def test_find_prompt_target_success(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", "my-project", "Shell")
    assert ok is True
    assert reason == ""
    assert pane_id == "%1"


def test_find_prompt_target_not_found(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%999", None, None)
    assert ok is False
    assert reason == "not_found"
    assert pane_id is None


def test_find_prompt_target_rejects_project_mismatch(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", "some-other-project", "Shell")
    assert ok is False
    assert reason == "mismatch"
    assert pane_id is None


def test_find_prompt_target_rejects_agent_mismatch(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", "my-project", "Codex")
    assert ok is False
    assert reason == "mismatch"


def test_find_prompt_target_rejects_dead_pane(monkeypatch, isolated_state):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: _pane_line(dead="1"))
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "my-project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    tower = tower_module.Tower(SESSION, own_pane_id="")
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "%1", None, None)
    assert ok is False
    assert reason == "dead"


def test_find_prompt_target_rejects_remote_rows(one_pane):
    tower = tower_module.Tower(SESSION, own_pane_id="")
    tower.rows = [{"key": "asus:%1", "remote": True, "status": "IDLE", "project": "p", "agent": "a"}]
    tower.load = lambda: None  # keep the manually-seeded remote row for this test
    ok, reason, pane_id = httpapi.find_prompt_target(tower, "asus:%1", None, None)
    assert ok is False
    assert reason == "remote_unsupported"


# -- HTTP integration (real ThreadingHTTPServer on an ephemeral port) ----


@pytest.fixture
def server(one_pane):
    srv = httpapi.create_server(
        "127.0.0.1", 0, SESSION, "", allowed_hosts=["localhost", "127.0.0.1"], token_path=one_pane / "tokens.json"
    )
    import threading

    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _url(server, path):
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}{path}"


def _get(server, path, token=None, host_header=None):
    req = urllib.request.Request(_url(server, path))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if host_header:
        req.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(server, path, body, token=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_url(server, path), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_requires_no_auth(server):
    status, body = _get(server, "/api/health")
    assert status == 200
    assert body["ok"] is True


def test_page_loads_without_auth(server):
    req = urllib.request.Request(_url(server, "/"))
    with urllib.request.urlopen(req, timeout=3) as resp:
        assert resp.status == 200
        assert b"<!doctype html>" in resp.read().lower()


def test_status_without_token_is_rejected(server):
    status, body = _get(server, "/api/status")
    assert status == 401
    assert body["ok"] is False


def test_pairing_then_status_works(server):
    code = server.pairing.current_code()
    status, body = _post(server, "/api/pair", {"code": code})
    assert status == 200
    token = body["token"]

    status, body = _get(server, "/api/status", token=token)
    assert status == 200
    assert len(body["panes"]) == 1


def test_wrong_pairing_code_rejected(server):
    status, body = _post(server, "/api/pair", {"code": "000000"})
    assert status == 400
    assert body["ok"] is False


def test_repeated_wrong_pairing_attempts_lock_out_the_code(server):
    from tmux_agent_tower.server.auth import MAX_PAIR_ATTEMPTS

    real_code = server.pairing.current_code()

    for _ in range(MAX_PAIR_ATTEMPTS):
        status, body = _post(server, "/api/pair", {"code": "000000"})
        assert status == 400
        assert body["error"] == "invalid_code"

    # The real code no longer works either -- locked out, not just the
    # wrong guesses rejected.
    status, body = _post(server, "/api/pair", {"code": real_code})
    assert status == 400
    assert body["error"] == "invalid_code"


def test_no_http_endpoint_can_regenerate_pairing_code(server):
    # Regenerating is local-only (a terminal keypress in main.py) --
    # there must be no reachable HTTP path to it at all.
    for path in ("/api/pair/regenerate", "/api/regenerate", "/api/pairing/new"):
        status, _ = _get(server, path)
        assert status == 404


def test_invalid_host_header_rejected(server):
    status, body = _get(server, "/api/health", host_header="evil.example.com")
    assert status == 400
    assert body["error"] == "invalid_host"


def test_prompt_send_calls_send_prompt_to_pane(server, monkeypatch):
    calls = []
    monkeypatch.setattr(httpapi, "send_prompt_to_pane", lambda pane_id, text: calls.append((pane_id, text)) or True)

    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    status, body = _post(
        server, "/api/prompt", {"pane_key": "%1", "text": "hello", "project": "my-project", "agent": "Shell"}, token=token
    )
    assert status == 200
    assert body["ok"] is True
    assert calls == [("%1", "hello")]


def test_prompt_send_without_auth_rejected(server, monkeypatch):
    calls = []
    monkeypatch.setattr(httpapi, "send_prompt_to_pane", lambda pane_id, text: calls.append((pane_id, text)) or True)

    status, body = _post(server, "/api/prompt", {"pane_key": "%1", "text": "hello"})
    assert status == 401
    assert calls == []


def test_prompt_too_long_is_rejected(server):
    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    status, body = _post(server, "/api/prompt", {"pane_key": "%1", "text": "a" * (httpapi.MAX_PROMPT_CHARS + 1)}, token=token)
    assert status == 413


def test_prompt_wrong_pane_key_rejected(server):
    code = server.pairing.current_code()
    _, body = _post(server, "/api/pair", {"code": code})
    token = body["token"]

    status, body = _post(server, "/api/prompt", {"pane_key": "%does-not-exist", "text": "hello"}, token=token)
    assert status == 409
    assert body["error"] == "not_found"


def test_unknown_path_is_404(server):
    status, body = _get(server, "/api/nonexistent")
    assert status == 404
