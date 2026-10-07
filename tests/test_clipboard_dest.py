"""Copy destination follows the client operating Tower."""

from tmux_agent_tower.adapters.base import ResultCandidate
from tmux_agent_tower.clipboard import CopyOutcome
from tmux_agent_tower.clipboard_dest import (
    COPY_TEST,
    LOCAL_HOST,
    CURRENT_TERMINAL,
    TMUX_BUFFER,
    CopyClient,
    SessionChoice,
    load_preference,
    override_applies,
    preference_configured,
    resolve_plan,
    run_copy_test,
    save_preference,
    should_offer_copy_setup,
    copy_test_notice,
)
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.i18n import t
from tmux_agent_tower.ui.control_view import _copy_notice, _copy_result, _copy_screen
from tmux_agent_tower.ui.settings_menu import copy_menu_items


BODY = "한글 결과\n둘째\n"


class _Tower:
    def __init__(self):
        self.results = ResultTracker()
        self.results.observe("%9", "IDLE", ResultCandidate(BODY, "fp-1"))
        self.rows = [{"key": "%9", "result_state": "ready", "remote": False}]
        self.local_host = "workstation-a"
        self.copy_override = None

    def load(self):
        return None


def _clients(*pairs):
    return tuple(pairs)


def _remote_workstation_access_context():
    from tmux_agent_tower.access_context import AccessContext

    return AccessContext("a" * 32, "workstation-b", "linux-wayland", "ssh", "workstation-b", "workstation-a-WSL", "bridge", 1.0, "live")


def test_local_access_context_uses_only_a_readback_capable_native_provider(monkeypatch):
    from types import SimpleNamespace
    from tmux_agent_tower.clipboard_dest import observe

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.resolve_terminal_target",
        lambda *_args, **_kwargs: SimpleNamespace(tty="/dev/pts/7", ambiguous=False, ssh=False),
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.context_for_process", lambda _pid: None)
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.native_clipboard_provider", lambda: "wl-copy")
    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.socket.gethostname", lambda: "access-host")

    client, clients = observe(["/dev/pts/7\t77\tattached"])

    assert clients == (("/dev/pts/7", 77),)
    assert client.access_context.transport == "local"
    assert client.access_context.access_host == "access-host"
    assert client.access_context.tower_host == "access-host"
    assert client.access_context.clipboard_capability == "host"


def test_missing_config_is_auto(tmp_path):
    path = tmp_path / "config.toml"
    assert load_preference(path) == "auto"
    assert preference_configured(path) is False
    path.write_text('language = "ko"\n', encoding="utf-8")
    assert load_preference(path) == "auto"
    assert preference_configured(path) is False


def test_save_keeps_other_lines_and_replaces_the_destination(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('language = "ko"\nproject_roots = ["/work"]\n', encoding="utf-8")
    save_preference(LOCAL_HOST, path)
    text = path.read_text(encoding="utf-8")
    assert 'language = "ko"' in text
    assert 'project_roots = ["/work"]' in text
    assert load_preference(path) == LOCAL_HOST
    save_preference("not-a-place", path)
    assert load_preference(path) == LOCAL_HOST
    save_preference("auto", path)
    assert load_preference(path) == "auto"
    assert text.count("language") == 1 or path.read_text(encoding="utf-8").count('language = "ko"') == 1


def test_existing_user_is_not_sent_through_copy_setup():
    assert should_offer_copy_setup(False, False) is False
    assert should_offer_copy_setup(True, False) is True
    assert should_offer_copy_setup(True, True) is False


def test_auto_local_client_uses_this_computer():
    client = CopyClient("/dev/pts/0", 10, False, False)
    plan = resolve_plan("auto", client, _clients(("/dev/pts/0", 10)))
    assert plan.destination == LOCAL_HOST and plan.ask is False and plan.source == "auto"


def test_auto_ssh_client_without_access_client_provider_asks():
    client = CopyClient("/dev/pts/3", 9, True, False)
    plan = resolve_plan("auto", client, _clients(("/dev/pts/3", 9)))
    assert plan.destination == "auto" and plan.ask is True and plan.source == "auto"


def test_auto_ssh_client_with_verified_bridge_uses_only_the_terminal():
    client = CopyClient("/dev/pts/3", 9, True, False, "a" * 32, _remote_workstation_access_context())
    plan = resolve_plan("auto", client, _clients(("/dev/pts/3", 9)))
    assert plan.destination == CURRENT_TERMINAL and plan.ask is False


def test_auto_ssh_copy_test_without_provider_writes_nothing(monkeypatch):
    captured = []

    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.copy_text", lambda *a, **k: captured.append(k))
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False), _clients(("/dev/pts/3", 9))),
    )
    assert run_copy_test("auto") == t("copy.choose_destination")
    assert captured == []


def test_auto_bridge_copy_test_passes_only_the_registered_endpoint(monkeypatch):
    from tmux_agent_tower.clipboard_bridge import BridgeRegistration

    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(True, False, "terminal")

    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.copy_text", fake_copy)
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False, "a" * 32, _remote_workstation_access_context()), _clients(("/dev/pts/3", 9))),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.load_bridge_client",
        lambda _instance: BridgeRegistration(43210, "private-token-012345678901234567890"),
    )
    assert run_copy_test("auto") == t("copy.test_sent")
    assert captured["text"] == COPY_TEST
    assert captured["destination"] == CURRENT_TERMINAL
    assert captured["bridge_port"] == 43210
    assert captured["bridge_token"] == "private-token-012345678901234567890"


def test_auto_ambiguous_client_asks_and_does_not_guess():
    client = CopyClient(None, None, False, True)
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    plan = resolve_plan("auto", client, clients)
    assert plan.ask is True and plan.destination == "auto"


def test_ambiguous_copy_test_requires_an_explicit_safe_destination(monkeypatch):
    called = []
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.copy_text",
        lambda *args, **kwargs: called.append(kwargs) or CopyOutcome(False, True, "tmux"),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient(None, None, False, True), clients),
    )
    assert run_copy_test("auto") == t("copy.choose_destination")
    assert called == []
    assert run_copy_test("auto", manual_destination=TMUX_BUFFER, manual_clients=clients) == t("control.copied_buffer")
    assert called[-1]["destination"] == TMUX_BUFFER
    assert run_copy_test("auto", manual_destination=LOCAL_HOST, manual_clients=()) == t("copy.choose_destination")


def test_auto_with_no_client_asks():
    plan = resolve_plan("auto", CopyClient(None, None, False, False), ())
    assert plan.ask is True


def test_saved_choice_overrides_what_auto_would_do():
    ssh = CopyClient("/dev/pts/3", 9, True, False)
    clients = _clients(("/dev/pts/3", 9))
    assert resolve_plan(LOCAL_HOST, ssh, clients).destination == LOCAL_HOST
    assert resolve_plan(CURRENT_TERMINAL, ssh, clients).destination == CURRENT_TERMINAL
    assert resolve_plan(TMUX_BUFFER, ssh, clients).destination == TMUX_BUFFER
    assert resolve_plan(LOCAL_HOST, ssh, clients).ask is False


def test_stale_client_override_is_ignored():
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    choice = SessionChoice(clients, LOCAL_HOST)
    assert override_applies(choice, clients) is True
    gone = _clients(("/dev/pts/1", 1))
    assert override_applies(choice, gone) is False
    replaced = _clients(("/dev/pts/1", 99), ("/dev/pts/2", 2))
    assert override_applies(choice, replaced) is False
    plan = resolve_plan("auto", CopyClient(None, None, False, True), gone, choice)
    assert plan.ask is True and plan.source == "auto"
    fresh = resolve_plan("auto", CopyClient(None, None, False, True), clients, choice)
    assert fresh.destination == LOCAL_HOST and fresh.ask is False and fresh.source == "session"


def test_messages_name_one_place_and_hide_the_mechanism():
    host = _copy_notice(CopyOutcome(True, False, "clip.exe"), "workstation-a")
    terminal = _copy_notice(CopyOutcome(True, False, "terminal"), "workstation-a")
    box = _copy_notice(CopyOutcome(False, True, "tmux"), "workstation-a")
    failed = _copy_notice(CopyOutcome(False, False, "none"), "workstation-a")
    assert host == "✓ workstation-a 클립보드에 복사했습니다"
    assert terminal == "✓ 현재 접속한 터미널에 복사했습니다"
    assert box == "✓ Tower 복사함에 저장했습니다"
    assert failed == "복사 위치를 확인해주세요"
    for notice in (host, terminal, box, failed):
        assert "OSC" not in notice
        assert "clip.exe" not in notice
        assert "tmux" not in notice.lower()
        assert "Windows" not in notice


def test_copy_test_does_not_claim_a_paste(monkeypatch):
    sent = {}

    def fake_copy(text, **kwargs):
        sent["text"] = text
        sent["destination"] = kwargs["destination"]
        return CopyOutcome(True, False, "clip.exe")

    monkeypatch.setattr("tmux_agent_tower.clipboard_dest.copy_text", fake_copy)
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient("/dev/pts/0", 10, False, False), _clients(("/dev/pts/0", 10))),
    )
    notice = run_copy_test("auto", host_label="workstation-a")
    assert sent["text"] == COPY_TEST
    assert sent["text"] != BODY
    assert sent["destination"] == LOCAL_HOST
    assert notice == t("copy.test_sent")
    assert "확인됨" not in notice
    assert "붙여넣기" in notice


def test_copy_test_does_not_guess_an_ambiguous_client(monkeypatch):
    called = []
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.copy_text",
        lambda *args, **kwargs: called.append(kwargs),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard_dest.observe",
        lambda: (CopyClient(None, None, False, True), _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))),
    )
    notice = run_copy_test("auto")
    assert called == []
    assert notice == t("copy.choose_destination")


def test_settings_menu_leads_with_automatic_and_offers_a_test():
    items = copy_menu_items()
    assert items[0] == ("auto", "자동으로 선택 (추천)")
    labels = [label for _key, label in items]
    assert labels[:4] == [
        "자동으로 선택 (추천)",
        "이 컴퓨터",
        "현재 접속한 터미널",
        "Tower 복사함",
    ]
    assert "복사 테스트" in labels


def test_manual_destination_is_what_y_writes(monkeypatch):
    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(False, True, "tmux")

    tower = _Tower()
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: TMUX_BUFFER)
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False), _clients(("/dev/pts/3", 9))),
    )
    notice = _copy_result(tower, "%9")
    assert captured["text"] == BODY
    assert captured["destination"] == TMUX_BUFFER
    assert notice == "✓ 최신 결과 전체를 Tower 복사함에 저장했습니다"
    assert tower.results.snapshot("%9").state == "ready"


def test_y_fails_closed_even_with_a_live_multi_client_override(monkeypatch):
    calls = []

    def fake_copy(text, **kwargs):
        calls.append(kwargs["destination"])
        return CopyOutcome(True, False, "clip.exe")

    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    tower = _Tower()
    tower.copy_override = SessionChoice(clients, LOCAL_HOST)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient(None, None, False, True), clients),
    )
    notice = _copy_result(tower, "%9")
    assert calls == []
    assert notice == "복사할 위치를 선택해주세요"

    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient(None, None, False, True), _clients(("/dev/pts/1", 1))),
    )
    notice = _copy_result(tower, "%9")
    assert calls == []
    assert notice == "복사할 위치를 선택해주세요"


def test_y_prompts_a_safe_destination_when_multiple_clients_are_attached(monkeypatch):
    calls = []
    prompts = []

    def fake_copy(text, **kwargs):
        calls.append(kwargs["destination"])
        return CopyOutcome(True, False, "clip.exe")

    tower = _Tower()
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._ask_copy_destination",
        lambda stdscr, *, allow_terminal=True: prompts.append((stdscr, allow_terminal)) or "local_host",
    )
    clients = _clients(("/dev/pts/1", 1), ("/dev/pts/2", 2))
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient(None, None, False, True), clients),
    )
    screen = object()
    notice = _copy_result(tower, "%9", stdscr=screen)
    assert calls == ["local_host"]
    assert prompts == [(screen, False)]
    assert notice == "✓ 최신 결과 전체를 workstation-a에 복사했습니다"
    assert tower.copy_override == SessionChoice(clients, LOCAL_HOST)


def test_y_uses_the_registered_remote_workstation_bridge_and_preserves_result_hash(monkeypatch):
    import hashlib

    from tmux_agent_tower.clipboard_bridge import BridgeRegistration

    captured = {}

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(True, False, "terminal")

    tower = _Tower()
    clients = _clients(("/dev/pts/3", 9))
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient("/dev/pts/3", 9, True, False, "a" * 32, _remote_workstation_access_context()), clients),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.load_bridge_client",
        lambda _instance: BridgeRegistration(43210, "private-token-012345678901234567890"),
    )
    notice = _copy_result(tower, "%9")
    assert captured["destination"] == CURRENT_TERMINAL
    assert captured["bridge_port"] == 43210
    assert captured["text"] == BODY
    assert hashlib.sha256(captured["text"].encode()).hexdigest() == hashlib.sha256(BODY.encode()).hexdigest()
    assert notice == "✓ 최신 결과 전체를 현재 접속한 터미널에 복사했습니다"
    assert tower.results.snapshot("%9").state == "read"


def test_remote_result_source_still_routes_to_local_access_clipboard(monkeypatch):
    from tmux_agent_tower.access_context import AccessContext

    payload = {
        "complete": True,
        "text": "원격 결과\n```python\nprint('workstation-b')\n```",
        "fingerprint": "remote-fingerprint",
        "source": "remote_tmux",
        "turn_identity": "workstation-b:$1:%9:123:remote-fingerprint",
        "timestamp": 1.0,
    }
    context = AccessContext(
        "b" * 32, "workstation-a", "windows-wsl", "local", "workstation-a", "workstation-a-WSL", "host", 1.0, "live"
    )
    captured = {}
    tower = _Tower()
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.get_result", lambda *_args: (True, "", payload))
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.observe",
        lambda: (CopyClient("/dev/pts/1", 4, False, False, access_context=context), _clients(("/dev/pts/1", 4))),
    )
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.load_preference", lambda: "auto")

    def fake_copy(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return CopyOutcome(True, False, "clip.exe")

    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", fake_copy)
    notice = _copy_result(tower, "workstation-b:%9")

    assert captured["text"] == payload["text"]
    assert captured["destination"] == LOCAL_HOST
    assert notice == "✓ 최신 결과 전체를 workstation-a에 복사했습니다"


def test_y_does_not_route_a_partial_tracker_candidate(monkeypatch):
    from tmux_agent_tower.adapters.base import ResultCandidate

    tower = _Tower()
    tower.results = ResultTracker()
    partial = ResultCandidate("tail fragment", "partial", confidence="partial")
    tower.results.observe("%9", "IDLE", partial)
    routed = []
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda *_args, **_kwargs: routed.append(True),
    )

    notice = _copy_result(tower, "%9")

    assert notice == "최신 결과 전체를 찾지 못했습니다"
    assert routed == []


def test_screen_copy_is_a_separate_explicit_payload(monkeypatch):
    routed = []
    tower = _Tower()
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view.get_pane_screen",
        lambda *_args, **_kwargs: (True, "", {"lines": ["visible one", "visible two"]}),
    )
    monkeypatch.setattr(
        "tmux_agent_tower.ui.control_view._route_clipboard",
        lambda _tower, text, _stdscr: routed.append(text) or (CopyOutcome(True, False, "clip.exe"), ""),
    )

    notice = _copy_screen(tower, "%9")

    assert routed == ["visible one\nvisible two"]
    assert notice == "✓ 현재 화면을 복사했습니다"


def test_terminal_send_is_not_described_as_a_confirmed_paste():
    notice = copy_test_notice(CopyOutcome(False, False, "terminal", True, False))
    assert "확인됨" not in notice
    assert "붙여넣기" in notice
