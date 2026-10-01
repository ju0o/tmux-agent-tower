"""Attention is a separate axis from execution and result.

False positives that must stay ``none``: an old approval left in
scrollback, a screen that is already working, the word Approve in a
README, a question the user typed, an old input question, and a question
inside a finished result.
"""

from tmux_agent_tower.adapters.base import PaneContext
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.adapters.grok import GrokAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.adapters.shell import ShellAdapter
from tmux_agent_tower.control import actions
from tmux_agent_tower.detection.attention import card_rank
from tmux_agent_tower.server import webui


def _ctx(lines, command=""):
    return PaneContext(title="", command=command, lines=tuple(lines))


def test_three_axes_can_be_set_together():
    row = {
        "status": "IDLE",
        "attention": "approval_required",
        "attention_prompt": "Allow this command to run?",
        "result_state": "ready",
    }
    assert card_rank(row) == 0
    assert row["status"] == "IDLE"
    assert row["result_state"] == "ready"
    assert row["attention"] == "approval_required"


def test_phone_sort_keeps_axes_and_orders_attention_first():
    rows = [
        {"key": "idle", "status": "IDLE", "attention": "none", "result_state": "none"},
        {"key": "work", "status": "WORKING", "attention": "none", "result_state": "none"},
        {"key": "ready", "status": "IDLE", "attention": "none", "result_state": "ready"},
        {"key": "ask", "status": "IDLE", "attention": "input_required", "result_state": "none"},
        {"key": "err", "status": "IDLE", "attention": "error", "result_state": "none"},
        {"key": "yes", "status": "IDLE", "attention": "approval_required", "result_state": "ready"},
        {"key": "dead", "status": "DEAD", "attention": "none", "result_state": "none"},
        {"key": "unk", "status": "UNKNOWN", "attention": "none", "result_state": "none"},
    ]
    ordered = [row["key"] for row in sorted(rows, key=card_rank)]
    assert ordered == ["yes", "ask", "ready", "err", "work", "idle", "unk", "dead"]


def test_codex_scrollback_approval_is_not_current(fixture_lines):
    old = list(fixture_lines("codex-waiting.txt"))
    old.extend(["", "Ask Codex to do anything"])
    ctx = _ctx(old, "codex")
    assert CodexAdapter().detect_attention(ctx) == "none"


def test_codex_already_working_is_not_approval():
    ctx = _ctx(
        [
            "Allow this command to run?",
            "1. Yes",
            "3. No",
            "Working (1m • esc to interrupt)",
        ],
        "codex",
    )
    assert CodexAdapter().detect_attention(ctx) == "none"
    assert CodexAdapter().approve(ctx) is None


def test_readme_approve_word_is_not_approval():
    ctx = _ctx(["Please Approve the design in this README before merging."], "codex")
    assert CodexAdapter().detect_attention(ctx) == "none"


def test_user_prompt_question_is_not_input():
    ctx = _ctx(["› Which format do you want?"], "codex")
    assert CodexAdapter().detect_attention(ctx) == "none"


def test_codex_question_inside_finished_result_is_not_attention():
    ctx = _ctx(
        [
            "Which format do you want?",
            "Worked for 12s",
            "Ask Codex to do anything",
        ],
        "codex",
    )
    assert CodexAdapter().detect_attention(ctx) == "none"
    assert CodexAdapter().detect_execution(ctx) == "IDLE"


def test_codex_trust_question_is_the_short_sentence_not_the_paragraph():
    """Live trust UI keeps the question and the policy on one line, and
    option 2 is Quit. The summary is the question. There is no reject key.
    """

    ctx = _ctx(
        [
            "  Trust this folder? Codex can read, edit, and run files here, subject to your permission settings.",
            "› 1. Trust and continue",
            "  2. Quit",
            "enter continue · esc quit",
        ],
        "codex",
    )
    adapter = CodexAdapter()
    assert adapter.detect_attention(ctx) == "approval_required"
    assert adapter.extract_attention_prompt(ctx) == "Trust this folder?"
    assert adapter.approve(ctx) == "Enter"
    assert adapter.reject(ctx) == "Escape"


def test_codex_live_command_menu_uses_one_not_y_or_p():
    """Codex 0.159 asks "Would you like to run the following command?"
    and labels the keys (y), (p), and (esc). Tower sends the numbered
    choice only: 1 to approve, 3 to reject. Never p, and never y.
    """

    gap = [""] * 8
    ctx = _ctx(
        [
            "Ask Codex to do anything",
            *gap,
            "Would you like to run the following command?",
            "printf marker > ok.txt",
            *gap,
            "› 1. Yes, proceed (y)",
            "  2. Yes, and don't ask again (p)",
            "  3. No, and tell Codex what to do differently (esc)",
        ],
        "codex",
    )
    adapter = CodexAdapter()
    assert adapter.detect_attention(ctx) == "approval_required"
    assert adapter.extract_attention_prompt(ctx) == "Would you like to run the following command?"
    assert adapter.approve(ctx) == "1"
    assert adapter.reject(ctx) == "3"


def test_codex_current_question_is_input_not_approval():
    ctx = _ctx(["Which format do you want?"], "codex")
    adapter = CodexAdapter()
    assert adapter.detect_attention(ctx) == "input_required"
    assert adapter.extract_attention_prompt(ctx) == "Which format do you want?"
    assert adapter.approve(ctx) is None


def test_attention_prompt_is_clamped():
    ctx = _ctx(["Q" * 400 + "?"], "codex")
    prompt = CodexAdapter().extract_attention_prompt(ctx)
    assert len(prompt) <= 160
    assert prompt.endswith("…")


def test_claude_folder_trust_is_approval_without_a_key():
    """The live picker highlights "No, exit" and has no 1/3 menu.
    Tower may show the question. It must not send a key.
    """

    ctx = _ctx(
        [
            "Quick safety check: Is this a project you created or one you trust?",
            "Claude Code'll be able to read, edit, and execute files here.",
            "❯ No, exit",
            "  Yes, I trust this folder",
        ],
        "claude",
    )
    adapter = ClaudeAdapter()
    assert adapter.detect_attention(ctx) == "approval_required"
    assert adapter.extract_attention_prompt(ctx) == "Is this a project you created or one you trust?"
    assert adapter.approve(ctx) is None
    assert adapter.reject(ctx) is None


def test_claude_cooked_result_question_is_not_current():
    ctx = _ctx(
        [
            "Do you want to proceed?",
            "1. Yes",
            "Cooked for 3m",
            "❯",
        ],
        "claude",
    )
    assert ClaudeAdapter().detect_attention(ctx) == "none"


def test_claude_user_question_is_not_input():
    ctx = _ctx(["❯ Which format?", "❯"], "claude")
    assert ClaudeAdapter().detect_attention(ctx) == "none"


def test_opencode_idle_chrome_hides_old_approval():
    ctx = _ctx(
        [
            "Do you want to allow this tool call?",
            "(y/n)",
            "ctrl+p commands",
        ],
        "opencode",
    )
    assert OpenCodeAdapter().detect_attention(ctx) == "none"


def test_opencode_working_hides_approval():
    ctx = _ctx(["allow this tool call?", "(y/n)", "esc interrupt"], "opencode")
    assert OpenCodeAdapter().detect_attention(ctx) == "none"


def test_cursor_followup_hides_old_allow():
    ctx = _ctx(["Run this command?", "Allow? (y/n)", "Add a follow-up"], "agent")
    assert CursorAdapter().detect_attention(ctx) == "none"
    assert CursorAdapter().approve(ctx) is None


def test_grok_and_shell_do_not_share_a_generic_detector():
    lines = ["Do you want to proceed?", "(y/n)", "Approve?"]
    assert GrokAdapter().detect_attention(_ctx(lines, "grok")) == "none"
    assert ShellAdapter().detect_attention(_ctx(lines, "bash")) == "none"
    assert GrokAdapter().approve(_ctx(lines, "grok")) is None


def test_trust_approve_sends_enter_and_not_a_digit(monkeypatch):
    captured = [
        "Trust this folder? Codex can read, edit, and run files here.",
        "› 1. Trust and continue",
        "  2. Quit",
        "enter continue · esc quit",
    ]
    sent = []
    monkeypatch.setattr(
        actions.tmux_capture,
        "run_tmux",
        lambda args, capture=True, timeout=3.0: "title\x1fcodex",
    )
    monkeypatch.setattr(
        actions.tmux_capture,
        "capture_pane",
        lambda pane_id, lines=40, body=tuple(captured): list(body),
    )
    monkeypatch.setattr(actions, "find_prompt_target", lambda *args: (True, "", "%15"))
    monkeypatch.setattr(
        actions, "send_attention_key", lambda pane, key: sent.append((pane, key)) or True
    )
    ok, reason = actions.respond_attention(object(), "%15", "approve")
    assert ok and reason == ""
    assert sent == [("%15", "Enter")]
    ok, reason = actions.respond_attention(object(), "%15", "reject")
    assert ok and reason == ""
    assert sent[-1] == ("%15", "Escape")


def test_send_attention_key_is_one_documented_character(monkeypatch):
    calls = []

    class _Result:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("shell", False)))
        return _Result()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    assert actions.send_attention_key("%4", "1") is True
    assert calls == [(["tmux", "send-keys", "-t", "%4", "1"], False)]
    assert actions.send_attention_key("%4", "Enter") is True
    assert calls[-1] == (["tmux", "send-keys", "-t", "%4", "Enter"], False)
    assert actions.send_attention_key("%4", "yes") is False
    assert actions.send_attention_key("%4", "y") is True
    assert actions.send_attention_key("%4", "Escape") is True
    assert calls[-1][0] == ["tmux", "send-keys", "-t", "%4", "Escape"]


def test_approve_sends_only_the_codex_key(monkeypatch, fixture_lines):
    captured = list(fixture_lines("codex-waiting.txt"))
    sent = []

    def fake_run(args, capture=True, timeout=3.0):
        if args and args[0] == "list-panes":
            return "title\x1fcodex"
        return ""

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake_run)
    monkeypatch.setattr(
        actions.tmux_capture,
        "capture_pane",
        lambda pane_id, lines=40, body=tuple(captured): list(body),
    )
    monkeypatch.setattr(actions, "find_prompt_target", lambda *args: (True, "", "%2"))
    monkeypatch.setattr(
        actions, "send_attention_key", lambda pane, key: sent.append((pane, key)) or True
    )
    ok, reason = actions.respond_attention(object(), "%2", "approve")
    assert ok and reason == ""
    assert sent == [("%2", "1")]


def test_opencode_approval_sends_nothing(monkeypatch, fixture_lines):
    captured = list(fixture_lines("opencode-waiting.txt"))
    sent = []

    def fake_run(args, capture=True, timeout=3.0):
        if args and args[0] == "list-panes":
            return "title\x1fopencode"
        return ""

    monkeypatch.setattr(actions.tmux_capture, "run_tmux", fake_run)
    monkeypatch.setattr(
        actions.tmux_capture,
        "capture_pane",
        lambda pane_id, lines=40, body=tuple(captured): list(body),
    )
    monkeypatch.setattr(actions, "find_prompt_target", lambda *args: (True, "", "%5"))
    monkeypatch.setattr(
        actions, "send_attention_key", lambda pane, key: sent.append((pane, key)) or True
    )
    ok, reason = actions.respond_attention(object(), "%5", "approve")
    assert not ok and reason == "approval_unknown"
    assert sent == []


def test_stale_approval_is_not_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(actions, "find_prompt_target", lambda *args: (True, "", "%2"))
    monkeypatch.setattr(
        actions.tmux_capture,
        "run_tmux",
        lambda args, capture=True, timeout=3.0: "title\x1fcodex",
    )
    monkeypatch.setattr(
        actions.tmux_capture,
        "capture_pane",
        lambda pane_id, lines=40: ["Ask Codex to do anything"],
    )
    monkeypatch.setattr(
        actions, "send_attention_key", lambda pane, key: sent.append(key) or True
    )
    ok, reason = actions.respond_attention(object(), "%2", "approve")
    assert not ok and reason == "not_approval"
    assert sent == []


def test_phone_buttons_are_not_the_card_click():
    html = webui.PAGE_HTML
    assert "! 승인 필요" in html
    assert "? 입력 필요" in html
    assert "✓ 새 Result" in html
    card = html.split('card.addEventListener("click"')[1].split("});")[0]
    assert "openDetail" in card
    assert "postAttention" not in card
    assert "approve" not in card
