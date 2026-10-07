"""Scratch-session regression against a real tmux server.

1. create scratch-A            4. delete scratch-A
2. bind a remote to it         5. status -> error/source_session_missing
3. local pane visible via API  6. API refuses instead of listing only the remote fixture

Runs on a private tmux socket so the developer's own tmux server is
never touched. Skipped when tmux is not installed.
"""

import json
import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid

import pytest

from tmux_agent_tower.server import httpapi, service
from tmux_agent_tower.tmux import capture as tmux_capture
from tmux_agent_tower.ui import tower as tower_module

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

SESSION = "scratch-A"


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    socket_dir = tmp_path / f"tmux-{uuid.uuid4().hex[:8]}"
    socket_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    created = subprocess.run(
        ["tmux", "new-session", "-d", "-s", SESSION, "-x", "80", "-y", "24", "sleep 300"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"could not start a private tmux server: {created.stderr.strip()}")

    try:
        yield socket_dir
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10, check=False)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "host").write_text("local-fixture\n", encoding="utf-8")
    (tmp_path / "config" / "remote-hosts.txt").write_text("remote-fixture.example:remote-fixture\n", encoding="utf-8")
    monkeypatch.setattr(
        tower_module, "fetch_remote",
        lambda alias, display_name=None, timeout=0: {
            "host": display_name, "status": tower_module.HOST_STATUS_ONLINE,
            "panes": [{"pane_id": "%1", "title": "remote-fixture-title", "command": "bash", "path": "/x", "dead": False, "lines": []}],
        },
    )
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path / "service")
    monkeypatch.setattr(service, "_status_cache", {"at": 0.0, "value": None})
    return tmp_path


def _kill_session(_socket_dir):
    subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True, timeout=10, check=False)


def _get(srv, path, token):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _pair(srv):
    data = json.dumps({"code": srv.pairing.current_code()}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/pair", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())["token"]


def test_session_exists_is_exact_against_a_real_server(private_tmux):
    assert tmux_capture.session_exists(SESSION) is True
    assert tmux_capture.session_exists("scratch") is False  # prefix must not match
    assert tmux_capture.session_exists("") is False


def test_scratch_session_lifecycle_end_to_end(private_tmux, isolated):
    srv = httpapi.create_server(
        "127.0.0.1", 0, SESSION, "", ["localhost", "127.0.0.1"], token_path=isolated / "tokens.json"
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        token = _pair(srv)

        # 3. the scratch session's own pane is a local row next to the remote fixture
        status, body = _get(srv, "/api/status", token)
        assert status == 200
        assert body["session"] == SESSION
        local = [p for p in body["panes"] if not p["remote"]]
        remote = [p for p in body["panes"] if p["remote"]]
        assert len(local) >= 1
        assert all(p["host"] == "local-fixture" for p in local)
        assert remote and remote[0]["host"] == "REMOTE-FIXTURE"

        # service.status() agrees while the session exists
        service._write_json(service._runtime_path(), {
            "pid": os.getpid(), "port": srv.server_address[1], "mode": "local",
            "url": f"http://127.0.0.1:{srv.server_address[1]}", "https": False, "ready": True,
            "tmux_session": SESSION, "own_pane_id": "",
        })
        real_ours = service.process_is_ours
        service.process_is_ours = lambda pid: True  # this pytest process stands in for the child
        try:
            before = service.status(force=True)
            assert before.state == "running"
            assert before.session == SESSION

            # 4. delete the session the remote is bound to
            _kill_session(private_tmux)
            assert tmux_capture.session_exists(SESSION) is False

            # 5. HTTP process and port are still up, but the remote is an error
            after = service.status(force=True)
            assert after.state == "error"
            assert after.reason == "source_session_missing"
            assert after.session == SESSION
        finally:
            service.process_is_ours = real_ours

        # 6. the API says so, and does not hand the phone a remote-only list
        status, body = _get(srv, "/api/status", token)
        assert status == 503
        assert body["error"] == "source_session_missing"
        assert body["session"] == SESSION
        assert "panes" not in body
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)
