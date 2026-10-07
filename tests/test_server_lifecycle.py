import socket
import threading

from tmux_agent_tower.server import httpapi
from tmux_agent_tower.server.netutil import detect_lan_ip


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_binds_to_localhost(tmp_path):
    port = _free_port()
    srv = httpapi.create_server(
        "127.0.0.1", port, "sess", "", allowed_hosts=["localhost", "127.0.0.1"], token_path=tmp_path / "tokens.json"
    )
    try:
        assert srv.server_address[0] == "127.0.0.1"
        assert srv.server_address[1] == port
    finally:
        srv.server_close()


def test_server_can_bind_all_interfaces_explicitly(tmp_path):
    port = _free_port()
    srv = httpapi.create_server(
        "0.0.0.0", port, "sess", "", allowed_hosts=["localhost", "127.0.0.1", "192.0.2.1"], token_path=tmp_path / "tokens.json"
    )
    try:
        assert srv.server_address[0] == "0.0.0.0"
    finally:
        srv.server_close()


def test_server_shuts_down_cleanly(tmp_path):
    port = _free_port()
    srv = httpapi.create_server(
        "127.0.0.1", port, "sess", "", allowed_hosts=["localhost", "127.0.0.1"], token_path=tmp_path / "tokens.json"
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    srv.shutdown()
    thread.join(timeout=2)
    srv.server_close()

    assert not thread.is_alive()


def test_detect_lan_ip_never_raises():
    # Best-effort: either a real IP string or None, but must never raise
    # even in a sandboxed/offline environment with no real network route.
    result = detect_lan_ip()
    assert result is None or isinstance(result, str)
