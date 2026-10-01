"""Prompt delivery is one bracketed paste and one Enter, then a check.

"Sent" and "submitted" are different answers. The phone keeps the text
when the submit was not confirmed, and nothing retries Enter blindly.
"""

from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.adapters.shell import ShellAdapter
from tmux_agent_tower.control import actions
from tmux_agent_tower.server import webui


class _Done:
    returncode = 0


def _record(calls):
    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("input"), kwargs.get("shell", False)))
        return _Done()

    return fake_run


KOREAN = "첫 줄 한글 프롬프트\n둘째 줄 특수문자 !@#$%^&*()[]{}<>|;'\"\\\n```python\nprint(\"hi\")\n```"


def test_send_text_is_one_bracketed_paste_with_the_exact_bytes(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.send_text("%7", KOREAN + "\n") is True
    assert len(calls) == 2
    load, paste = calls
    assert load[0][:3] == ["tmux", "load-buffer", "-b"] and load[0][-1] == "-"
    assert load[1] == KOREAN.encode("utf-8")
    assert paste[0][:4] == ["tmux", "paste-buffer", "-d", "-p"]
    assert paste[0][-2:] == ["-t", "%7"]
    assert all(call[2] is False for call in calls)


def test_long_text_is_passed_through_stdin_not_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    long_text = ("가나다 abc " * 400).strip()
    assert actions.send_text("%7", long_text)
    assert calls[0][1] == long_text.encode("utf-8")
    assert long_text not in " ".join(calls[0][0])


def test_submit_input_sends_enter_once_and_refuses_other_keys(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.submit_input("%7") is True
    assert calls == [(["tmux", "send-keys", "-t", "%7", "Enter"], None, False)]
    assert actions.submit_input("%7", "C-m") is False
    assert actions.submit_input("%7", "y") is False
    assert len(calls) == 1


def test_send_prompt_to_pane_never_doubles_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", _record(calls))
    assert actions.send_prompt_to_pane("%7", "hello")
    enters = [c for c in calls if c[0][:2] == ["tmux", "send-keys"]]
    assert len(enters) == 1
    assert enters[0][0][-1] == "Enter"


def _prompt_flow(monkeypatch, screens, agent_cmd="codex"):
    """Drive send_prompt with a scripted sequence of screens."""

    frames = list(screens)
    sent = []

    def fake_run_tmux(args, capture=True, timeout=3.0):
        if args and args[0] == "list-panes":
            return f"title\x1f{agent_cmd}"
        return ""

    def fake_capture(pane_id, lines=40):
        if len(frames) > 1:
            return list(frames.pop(0))
        return list(frames[0])

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake_run_tmux)
    monkeypatch.setattr(actions.tmux_capture, "capture_pane", fake_capture)
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (True, "", "%7"))
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda pane, text, key="Enter": sent.append((pane, text, key)) or True)
    monkeypatch.setattr(actions, "SUBMIT_CONFIRM_SECONDS", 0.3)
    monkeypatch.setattr(actions, "SUBMIT_POLL_SECONDS", 0.01)
    return sent


def test_submit_confirmed_when_codex_starts_working(monkeypatch):
    before = ["› Ask Codex to do anything"]
    after = ["› Reply with pong", "Working (1s • esc to interrupt)"]
    sent = _prompt_flow(monkeypatch, [before, after])
    out = actions.send_prompt(object(), "%7", "Reply with pong")
    assert out.ok and out.submitted and out.reason == ""
    assert sent == [("%7", "Reply with pong", "Enter")]


def test_submit_not_confirmed_when_text_stays_in_the_composer(monkeypatch):
    before = ["› Ask Codex to do anything"]
    stuck = ["› Reply with pong"]
    sent = _prompt_flow(monkeypatch, [before, stuck, stuck])
    out = actions.send_prompt(object(), "%7", "Reply with pong")
    assert out.ok is True
    assert out.submitted is False
    assert out.reason == "submit_not_confirmed"
    assert len(sent) == 1


def test_stale_pane_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "find_prompt_target", lambda *a: (False, "stale", None))
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *a, **k: sent.append(a) or True)
    out = actions.send_prompt(object(), "%7", "hello")
    assert not out.ok and out.reason == "stale"
    assert sent == []


def test_blank_text_sends_nothing(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "send_prompt_to_pane", lambda *a, **k: sent.append(a) or True)
    out = actions.send_prompt(object(), "%7", "   \n")
    assert not out.ok and out.reason == "missing_text"
    assert sent == []


def _ctx(lines, command):
    return PaneContext(title="", command=command, lines=tuple(lines))


def test_codex_fast_turn_is_confirmed_by_echo_and_worked_for():
    before = _ctx(["› Ask Codex to do anything"], "codex")
    after = _ctx(["› Reply with exactly: pong 1", "• pong 1", "Worked for 1s", "› Ask Codex to do anything"], "codex")
    assert CodexAdapter().confirm_submitted(before, after, "Reply with exactly: pong 1") is True


def test_codex_composer_holding_text_is_not_confirmed():
    before = _ctx(["› Ask Codex to do anything"], "codex")
    stuck = _ctx(["› Reply with exactly: pong 1"], "codex")
    assert CodexAdapter().confirm_submitted(before, stuck, "Reply with exactly: pong 1") is None


def test_claude_two_second_turn_is_confirmed_by_a_new_done_line():
    before = _ctx(["❯ Try \"fix typecheck errors\"", "⏸ manual mode on · ? for shortcuts"], "claude")
    after = _ctx(
        ["❯ Reply with pong", "● pong", "✻ Sautéed for 2s · done 5:19 PM", "❯", "⏸ manual mode on · ? for shortcuts"],
        "claude",
    )
    assert ClaudeAdapter().confirm_submitted(before, after, "Reply with pong") is True


def test_claude_result_is_found_for_any_done_verb_not_only_cooked():
    lines = [
        "❯ Reply with pong",
        "● pong 5",
        "✻ Brewed for 2s · done 5:36 PM",
        "────────",
        "❯ ",
        "────────",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    found = ClaudeAdapter().extract_result(PaneContext(title="✳ Claude Code", command="claude", lines=tuple(lines)))
    assert found is not None and "pong 5" in found.text


def test_claude_ghost_suggestion_in_the_box_is_idle_and_keeps_the_result():
    lines = [
        "● MULTI-1",
        "✻ Baked for 2s · done 5:41 PM",
        "────────",
        "❯ pong 7",
        "────────",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    ctx = PaneContext(title="✳ Claude Code", command="claude", lines=tuple(lines))
    assert ClaudeAdapter().detect_execution(ctx) == "IDLE"
    found = ClaudeAdapter().extract_result(ctx)
    assert found is not None and "MULTI-1" in found.text


def test_claude_conversation_title_glyph_is_static_and_result_is_last_turn_only():
    lines = [
        "❯ Reply with exactly: pong 5",
        "● pong 5",
        "✻ Brewed for 2s · done 5:36 PM",
        "❯ Reply with exactly: TITLE-PROBE then list three colors",
        "● TITLE-PROBE",
        "  Red",
        "  Green",
        "✻ Crunched for 2s · done 5:46 PM",
        "❯ ",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
    ctx = PaneContext(title="✳ Pong 1", command="claude", lines=tuple(lines))
    adapter = ClaudeAdapter()
    assert adapter.detect_execution(ctx) == "IDLE"
    assert adapter.detect_attention(ctx) == "none"
    found = adapter.extract_result(ctx)
    assert found is not None
    assert "TITLE-PROBE" in found.text and "pong 5" not in found.text


def test_claude_prose_mentioning_waited_for_is_not_a_turn_end():
    lines = ["❯ explain", "● The job waited for 3s before it ran, then I waited for 2m more.", "❯ "]
    assert ClaudeAdapter().extract_result(PaneContext(title="✳ Claude Code", command="claude", lines=tuple(lines))) is None


def test_claude_text_sitting_in_the_box_is_not_confirmed():
    before = _ctx(["❯ Try \"fix typecheck errors\""], "claude")
    stuck = _ctx(["❯ Reply with pong", "⏸ manual mode on · ? for shortcuts"], "claude")
    assert ClaudeAdapter().confirm_submitted(before, stuck, "Reply with pong") is None


def test_claude_static_title_glyph_is_not_working(fixture_lines):
    ctx = PaneContext(title="✳ Claude Code", command="claude", lines=("❯ Try \"refactor x\"", "⏸ manual mode on · ? for shortcuts"))
    assert ClaudeAdapter().detect_execution(ctx) == "IDLE"
    assert ClaudeAdapter().classify(_ctx(list(fixture_lines("claude-working.txt")), "claude")).status == "WORKING"


def test_opencode_confirmed_by_message_block_or_turn_line():
    before = _ctx(["tab agents  ctrl+p commands"], "opencode")
    running = _ctx(["┃  Reply with pong", "⬝⬝⬝⬝  esc interrupt"], "opencode")
    done = _ctx(["┃  Reply with pong", "pong", "▣  Build · model · 5.2s", "ctrl+p commands"], "opencode")
    adapter = OpenCodeAdapter()
    assert adapter.confirm_submitted(before, running, "Reply with pong") is True
    assert adapter.confirm_submitted(before, done, "Reply with pong") is True


def test_opencode_tall_pane_idle_hint_is_still_idle():
    lines = ["┃  Build · model"] + [""] * 10 + ["tab agents  ctrl+p commands"] + [""] * 40 + ["<path>  1.18.32"]
    assert OpenCodeAdapter().detect_execution(_ctx(lines, "opencode")) == "IDLE"


def test_shell_has_no_submit_evidence_by_default():
    before = _ctx(["user@host:~$"], "bash")
    after = _ctx(["user@host:~$ ls", "a b c", "user@host:~$"], "bash")
    assert ShellAdapter().confirm_submitted(before, after, "ls") is None


def test_capture_pane_drops_trailing_blank_rows(monkeypatch):
    from tmux_agent_tower.tmux import capture

    monkeypatch.setattr(capture, "run_tmux", lambda args, capture=True, timeout=3.0: "a\nb\n\n   \n")
    assert capture.capture_pane("%1") == ["a", "b"]


def test_phone_windows_are_labels_and_only_pane_cards_open():
    html = webui.PAGE_HTML
    render = html.split("function render(")[1].split("function pollList")[0]
    label = render.split('label.className = "window-label"')[1].split("items.forEach")[0]
    assert "addEventListener" not in label
    assert "openDetail(p.key)" in render
    assert "창 " in render


def test_pairing_is_not_kicked_back_by_an_unauthenticated_status_poll():
    html = webui.PAGE_HTML
    poll = html.split("function pollList()")[1].split("function fillDetailMeta")[0]
    assert "if (document.hidden || !token) return;" in poll
    assert "if (token !== sentToken) return;" in poll
    boot = html.split("document.addEventListener(\"visibilitychange\"")[1]
    assert boot.count("startTimers();") == 2
    assert "if (token)" in boot


def test_phone_keeps_text_unless_submit_was_confirmed():
    html = webui.PAGE_HTML
    handler = html.split('sendBtn.addEventListener("click"')[1].split("var sendStateTimer")[0]
    confirmed = handler.split("res.body.submitted")[1].split("else if")[0]
    unconfirmed = handler.split("else if (res.status === 200 && res.body.ok)")[1].split("} else {")[0]
    assert 'promptText.value = ""' in confirmed
    assert 'promptText.value = ""' not in unconfirmed
    assert "제출 여부를 확인하지 못했습니다" in unconfirmed
    assert "전송 중..." in handler
    assert "if (!current || sending) return;" in handler
