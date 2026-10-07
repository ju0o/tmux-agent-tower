import json

from tmux_agent_tower.server import tailscale as ts

FAKE_EXE = "fake-tailscale.exe"
FAKE_STATUS = {
    "Self": {
        "DNSName": "mybox.tailxxxxx.ts.net.",
        "TailscaleIPs": ["100.64.1.2", "fd7a:115c:a1e0::1"],
    }
}


class _FakeResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _mock_run(monkeypatch, responses):
    """``responses`` maps a tuple of args to a _FakeResult (or exception
    class to raise). Falls back to a generic success with empty stdout
    for anything not listed.
    """

    def fake_run(cmd, **kwargs):
        args = tuple(cmd[1:])  # drop the exe path
        for key, value in responses.items():
            if args == key:
                if isinstance(value, Exception):
                    raise value
                return value
        return _FakeResult("")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)


# -- find_tailscale_exe ---------------------------------------------------


def test_find_tailscale_exe_missing_returns_none(monkeypatch):
    monkeypatch.setattr(ts.Path, "exists", lambda self: False)
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    assert ts.find_tailscale_exe() is None


def test_find_tailscale_exe_prefers_windows_path(monkeypatch):
    monkeypatch.setattr(ts.Path, "exists", lambda self: True)
    assert ts.find_tailscale_exe() == ts._WINDOWS_TAILSCALE_PATH


def test_find_tailscale_exe_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(ts.Path, "exists", lambda self: False)
    monkeypatch.setattr(ts.shutil, "which", lambda name: "/usr/bin/tailscale" if name == "tailscale" else None)
    assert ts.find_tailscale_exe() == "/usr/bin/tailscale"


# -- status parsing ---------------------------------------------------


def test_self_dns_name_strips_trailing_dot():
    assert ts.self_dns_name(FAKE_STATUS) == "mybox.tailxxxxx.ts.net"


def test_self_dns_name_none_when_absent():
    assert ts.self_dns_name({"Self": {}}) is None


def test_self_tailscale_ip_prefers_ipv4():
    assert ts.self_tailscale_ip(FAKE_STATUS) == "100.64.1.2"


def test_self_tailscale_ip_none_when_absent():
    assert ts.self_tailscale_ip({"Self": {"TailscaleIPs": []}}) is None


def test_get_status_returns_none_on_bad_json(monkeypatch):
    _mock_run(monkeypatch, {("status", "--json"): _FakeResult("not json")})
    assert ts.get_status(FAKE_EXE) is None


def test_get_status_returns_none_on_nonzero_exit(monkeypatch):
    _mock_run(monkeypatch, {("status", "--json"): _FakeResult("{}", returncode=1)})
    assert ts.get_status(FAKE_EXE) is None


def test_get_status_returns_none_on_exception(monkeypatch):
    _mock_run(monkeypatch, {("status", "--json"): TimeoutError("boom")})
    assert ts.get_status(FAKE_EXE) is None


# -- existing_mapping_proxy ---------------------------------------------------


def test_existing_mapping_proxy_none_when_no_serve_config():
    assert ts.existing_mapping_proxy(None, "mybox.tailxxxxx.ts.net") is None
    assert ts.existing_mapping_proxy({}, "mybox.tailxxxxx.ts.net") is None


def test_existing_mapping_proxy_finds_configured_target():
    serve_status = {
        "Web": {
            "mybox.tailxxxxx.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4312"}}},
        }
    }
    assert ts.existing_mapping_proxy(serve_status, "mybox.tailxxxxx.ts.net") == "http://127.0.0.1:4312"


# -- start_serve -----------------------------------------------------------


def test_start_serve_fails_when_status_unavailable(monkeypatch):
    _mock_run(monkeypatch, {("status", "--json"): _FakeResult("", returncode=1)})
    ok, detail = ts.start_serve(FAKE_EXE, 4312)
    assert ok is False
    assert detail == "tailscale_status_unavailable"


def test_start_serve_fails_when_no_magicdns(monkeypatch):
    _mock_run(monkeypatch, {("status", "--json"): _FakeResult(json.dumps({"Self": {}}))})
    ok, detail = ts.start_serve(FAKE_EXE, 4312)
    assert ok is False
    assert detail == "no_magicdns"


def test_start_serve_refuses_to_overwrite_existing_unrelated_mapping(monkeypatch):
    serve_status = {"Web": {"mybox.tailxxxxx.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}}
    _mock_run(
        monkeypatch,
        {
            ("status", "--json"): _FakeResult(json.dumps(FAKE_STATUS)),
            ("serve", "status", "--json"): _FakeResult(json.dumps(serve_status)),
        },
    )
    ok, detail = ts.start_serve(FAKE_EXE, 4312)
    assert ok is False
    assert detail == "existing_mapping:http://127.0.0.1:9999"


def test_start_serve_never_calls_funnel(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        if cmd[1:] == ["status", "--json"]:
            return _FakeResult(json.dumps(FAKE_STATUS))
        if cmd[1:3] == ["serve", "status"]:
            return _FakeResult(json.dumps({}))
        return _FakeResult("")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    ts.start_serve(FAKE_EXE, 4312)

    assert not any("funnel" in c for call in calls for c in call)


def test_start_serve_never_calls_reset(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        if cmd[1:] == ["status", "--json"]:
            return _FakeResult(json.dumps(FAKE_STATUS))
        if cmd[1:3] == ["serve", "status"]:
            return _FakeResult(json.dumps({}))
        return _FakeResult("")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    ts.start_serve(FAKE_EXE, 4312)

    assert not any("reset" in c for call in calls for c in call)


def test_start_serve_succeeds_and_returns_dns_name(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[1:] == ["status", "--json"]:
            return _FakeResult(json.dumps(FAKE_STATUS))
        if cmd[1:3] == ["serve", "status"]:
            return _FakeResult(json.dumps({}))
        if cmd[1:3] == ["serve", "--bg"]:
            return _FakeResult("")
        return _FakeResult("", returncode=1)

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    ok, detail = ts.start_serve(FAKE_EXE, 4312)
    assert ok is True
    assert detail == "mybox.tailxxxxx.ts.net"


def test_start_serve_is_idempotent_when_already_correctly_configured(monkeypatch):
    serve_status = {"Web": {"mybox.tailxxxxx.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4312"}}}}}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        if cmd[1:] == ["status", "--json"]:
            return _FakeResult(json.dumps(FAKE_STATUS))
        if cmd[1:3] == ["serve", "status"]:
            return _FakeResult(json.dumps(serve_status))
        return _FakeResult("")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    ok, detail = ts.start_serve(FAKE_EXE, 4312)
    assert ok is True
    assert detail == "mybox.tailxxxxx.ts.net"
    # Must not have issued a new `serve --bg` call -- nothing to change.
    assert ("serve", "--bg", "4312") not in calls


# -- stop_serve --------------------------------------------------------------


def test_stop_serve_removes_our_own_mapping(monkeypatch):
    serve_status = {"Web": {"mybox.tailxxxxx.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4312"}}}}}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        if cmd[1:] == ["status", "--json"]:
            return _FakeResult(json.dumps(FAKE_STATUS))
        if cmd[1:3] == ["serve", "status"]:
            return _FakeResult(json.dumps(serve_status))
        return _FakeResult("")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    assert ts.stop_serve(FAKE_EXE, 4312) is True
    assert ("serve", "--https=443", "off") in calls


def test_stop_serve_refuses_when_mapping_is_not_ours(monkeypatch):
    serve_status = {"Web": {"mybox.tailxxxxx.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        if cmd[1:] == ["status", "--json"]:
            return _FakeResult(json.dumps(FAKE_STATUS))
        if cmd[1:3] == ["serve", "status"]:
            return _FakeResult(json.dumps(serve_status))
        return _FakeResult("")

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    assert ts.stop_serve(FAKE_EXE, 4312) is False
    assert ("serve", "--https=443", "off") not in calls


def test_stop_serve_never_calls_reset(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd[1:]))
        return _FakeResult("{}", returncode=1)

    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    ts.stop_serve(FAKE_EXE, 4312)
    assert not any("reset" in c for call in calls for c in call)


# -- windows_can_reach_backend ---------------------------------------------


def test_windows_can_reach_backend_none_when_curl_missing(monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    assert ts.windows_can_reach_backend(4312) is None


def test_windows_can_reach_backend_true_on_200(monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: "/mnt/c/Windows/System32/curl.exe")
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: _FakeResult("200"))
    assert ts.windows_can_reach_backend(4312) is True


def test_windows_can_reach_backend_false_on_non_200(monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: "/mnt/c/Windows/System32/curl.exe")
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: _FakeResult("000"))
    assert ts.windows_can_reach_backend(4312) is False


def test_windows_can_reach_backend_false_on_exception(monkeypatch):
    monkeypatch.setattr(ts.shutil, "which", lambda name: "/mnt/c/Windows/System32/curl.exe")

    def raise_run(*a, **k):
        raise TimeoutError("boom")

    monkeypatch.setattr(ts.subprocess, "run", raise_run)
    assert ts.windows_can_reach_backend(4312) is False


def test_windows_can_reach_backend_never_leaks_raw_transcript(monkeypatch):
    # The verification call only ever asks for /api/health's status code
    # -- it must never request or handle full pane/transcript content.
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeResult("200")

    monkeypatch.setattr(ts.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(ts.subprocess, "run", fake_run)
    ts.windows_can_reach_backend(4312)

    assert any("/api/health" in str(c) for c in calls[0])
    assert not any("/api/status" in str(c) for c in calls[0])
