import hashlib
import json
import socket
import stat
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tmux_agent_tower.clipboard_bridge import (
    ClipboardBridgeServer,
    MAX_BYTES,
    _copy_with_wl_copy,
    _create_token_state,
    load_bridge_client,
    register_bridge_client,
    unregister_bridge_client,
)


TOKEN = "test-bridge-token"


def _start(provider=lambda _data: True):
    server = ClipboardBridgeServer(TOKEN, provider=provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post(server, body=b"", token=TOKEN, path="/copy"):
    host, port = server.server_address
    headers = {} if token is None else {"X-Tower-Token": token}
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as error:
        response = error
    return response.code, json.loads(response.read())


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_bridge_binds_only_to_loopback_and_serves_post_copy():
    server, thread = _start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        status, result = _post(server, b"hello")
        assert status == 200
        assert set(result) == {"success", "payload_sha256"}
    finally:
        _stop(server, thread)


def test_token_required_and_wrong_token_rejected():
    calls = []
    server, thread = _start(lambda data: calls.append(data) or True)
    try:
        assert _post(server, b"x", token=None)[0] == 401
        assert _post(server, b"x", token="wrong")[0] == 401
        assert calls == []
    finally:
        _stop(server, thread)


def test_only_post_copy_is_exposed():
    server, thread = _start()
    try:
        assert _post(server, b"x", path="/other") == (404, {"error": "not_found"})
        host, port = server.server_address
        request = urllib.request.Request(f"http://{host}:{port}/copy", method="GET")
        with pytest.raises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(request, timeout=2)
        assert result.value.code == 405
        assert json.loads(result.value.read()) == {"error": "method_not_allowed"}
    finally:
        _stop(server, thread)


def test_request_size_cap_rejects_without_calling_provider():
    calls = []
    server, thread = _start(lambda data: calls.append(data) or True)
    try:
        status, result = _post(server, b"x" * (MAX_BYTES + 1))
        assert (status, result) == (413, {"error": "too_large"})
        assert calls == []
    finally:
        _stop(server, thread)


def test_utf8_multiline_code_and_exact_payload_hash():
    payload = "한글 제목\n\n```python\nprint('tower')\n```\n둘째 줄".encode("utf-8")
    copied = []
    server, thread = _start(lambda data: copied.append(data) or True)
    try:
        status, result = _post(server, payload)
        assert status == 200
        assert copied == [payload]
        assert result == {"success": True, "payload_sha256": hashlib.sha256(payload).hexdigest()}
    finally:
        _stop(server, thread)


def test_invalid_utf8_and_nul_are_rejected():
    server, thread = _start()
    try:
        assert _post(server, b"\xff") == (400, {"error": "invalid_text"})
        assert _post(server, b"a\0b") == (400, {"error": "invalid_text"})
    finally:
        _stop(server, thread)


def test_request_and_token_are_never_logged(capsys):
    body = b"PRIVATE_SYNTHETIC_BODY"
    server, thread = _start()
    try:
        _post(server, body)
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert body.decode() not in output
        assert TOKEN not in output
    finally:
        _stop(server, thread)


def test_provider_failure_is_an_error():
    server, thread = _start(lambda _data: False)
    try:
        assert _post(server, b"x") == (502, {"error": "provider_failed"})
    finally:
        _stop(server, thread)


def test_provider_uses_fixed_wl_copy_argv_and_timeout(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        if argv == ["wl-copy"]:
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=b"synthetic")

    monkeypatch.setattr("tmux_agent_tower.clipboard_bridge.subprocess.run", run)
    assert _copy_with_wl_copy(b"synthetic") is True
    assert seen[0][0] == ["wl-copy"]
    assert seen[0][1]["input"] == b"synthetic"
    assert seen[0][1]["timeout"] == 8
    assert "shell" not in seen[0][1]
    assert seen[0][1]["stdout"] is seen[0][1]["stderr"]
    assert seen[1][0] == ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"]
    assert seen[1][1]["timeout"] == 8


def test_provider_requires_exact_wayland_readback(monkeypatch):
    calls = iter((SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout=b"different")))
    monkeypatch.setattr("tmux_agent_tower.clipboard_bridge.subprocess.run", lambda *_args, **_kwargs: next(calls))
    assert _copy_with_wl_copy(b"expected") is False


def test_generic_provider_uses_native_host_clipboard_without_buffer_fallback(monkeypatch):
    from tmux_agent_tower.clipboard_bridge import _copy_with_native_clipboard

    calls = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.copy_host_clipboard",
        lambda text: calls.append(text)
        or SimpleNamespace(clipboard=True),
    )
    assert _copy_with_native_clipboard("한글".encode()) is True
    assert calls == ["한글"]


def test_token_state_has_private_permissions():
    state_dir, token_file, token = _create_token_state()
    try:
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert token_file.read_text(encoding="ascii") == token
    finally:
        import shutil

        shutil.rmtree(state_dir)


def test_bridge_registration_is_private_and_removed(monkeypatch, tmp_path):
    directory = tmp_path / "bridge-state"
    monkeypatch.setattr("tmux_agent_tower.clipboard_bridge.bridge_state_dir", lambda: directory)
    instance_id = "a" * 32
    token = "private-test-token-that-is-long-enough"
    register_bridge_client(instance_id, {"port": 43210, "token": token})
    path = directory / f"{instance_id}.json"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    registration = load_bridge_client(instance_id)
    assert registration is not None
    assert registration.port == 43210
    assert registration.token == token
    unregister_bridge_client(instance_id)
    assert load_bridge_client(instance_id) is None


def test_bridge_registration_carries_access_client_identity(monkeypatch, tmp_path):
    directory = tmp_path / "bridge-state"
    monkeypatch.setattr("tmux_agent_tower.clipboard_bridge.bridge_state_dir", lambda: directory)
    instance_id = "c" * 32
    context = {
        "client_id": instance_id,
        "display_name": "workstation-b",
        "platform": "linux-wayland",
        "transport": "ssh",
        "access_host": "workstation-b",
        "tower_host": "workstation-a-WSL",
        "clipboard_capability": "bridge",
        "registration_timestamp": 1234.0,
        "liveness": "live",
    }
    register_bridge_client(instance_id, {"port": 43210, "token": TOKEN * 2, "access_context": context})
    registration = load_bridge_client(instance_id)
    assert registration.access_context.display_name == "workstation-b"
    assert registration.access_context.access_host == "workstation-b"
    assert registration.access_context.tower_host == "workstation-a-WSL"
    assert registration.access_context.clipboard_capability == "bridge"
    unregister_bridge_client(instance_id)


def test_bridge_registration_rejects_invalid_identity_and_shared_permissions(monkeypatch, tmp_path):
    directory = tmp_path / "bridge-state"
    monkeypatch.setattr("tmux_agent_tower.clipboard_bridge.bridge_state_dir", lambda: directory)
    with pytest.raises(ValueError):
        register_bridge_client("../bad", {"port": 1234, "token": TOKEN})
    instance_id = "b" * 32
    register_bridge_client(instance_id, {"port": 1234, "token": TOKEN * 2})
    path = directory / f"{instance_id}.json"
    path.chmod(0o640)
    assert load_bridge_client(instance_id) is None


def test_shutdown_stops_serving_and_releases_loopback_port():
    server, thread = _start()
    address = server.server_address
    _stop(server, thread)
    assert not thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.2)
