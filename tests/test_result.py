import json
from pathlib import Path

from tmux_agent_tower.adapters.base import PaneContext, prose_body
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.adapters.grok import GrokAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.adapters.shell import ShellAdapter
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.server.httpapi import build_status_payload
from tmux_agent_tower.server.webui import PAGE_HTML

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def lines(name: str):
    return tuple(FIXTURES.joinpath(name).read_text(encoding="utf-8").splitlines())


def ctx(name: str, title: str = "", command: str = ""):
    return PaneContext(title=title, command=command, lines=lines(name))


def test_codex_idle_extracts_the_answer_not_the_finished_bullet():
    result = CodexAdapter().extract_result(ctx("codex-idle.txt"))
    assert result is not None
    assert result.text == "Summary of what changed goes here."
    assert "Finished" not in result.text


def test_codex_working_is_not_a_result():
    assert CodexAdapter().extract_result(ctx("codex-working.txt", title="⠙ working")) is None


def test_claude_tool_bullet_alone_is_not_a_result():
    assert ClaudeAdapter().extract_result(ctx("claude-idle-with-last-action.txt")) is None


def test_claude_prose_before_cooked_is_the_result():
    screen = [
        "Session binding now follows the Tower that started it.",
        "✻ Cooked for 4m 2s",
        "❯",
    ]
    result = ClaudeAdapter().extract_result(PaneContext(title="", command="claude", lines=tuple(screen)))
    assert result is not None
    assert result.text.startswith("Session binding")


def test_opencode_tool_summary_is_not_a_result_but_prose_is():
    assert OpenCodeAdapter().extract_result(ctx("opencode-idle.txt")) is None
    screen = [
        "The promo file is in out/JuPortal_Promo.mp4.",
        "▣  Build · Space Bunny Free · 6.0s",
        "ctrl+p commands",
    ]
    result = OpenCodeAdapter().extract_result(PaneContext(title="", command="opencode", lines=tuple(screen)))
    assert result is not None
    assert "promo file" in result.text


def test_opencode_working_is_not_a_result():
    assert OpenCodeAdapter().extract_result(ctx("opencode-working.txt")) is None


def test_cursor_followup_keeps_the_answer_and_drops_finished():
    result = CursorAdapter().extract_result(ctx("cursor-idle-followup.txt"))
    assert result is not None
    assert result.text == "All tests passed."


def test_cursor_still_running_is_not_a_result():
    screen = ["All tests passed.", "→ Add a follow-up", "ctrl+c to stop"]
    assert CursorAdapter().extract_result(PaneContext(title="", command="agent", lines=tuple(screen))) is None


def test_grok_and_shell_do_not_invent_a_result():
    assert GrokAdapter().extract_result(ctx("grok-idle.txt")) is None
    assert GrokAdapter().extract_result(ctx("grok-working.txt")) is None
    assert ShellAdapter().extract_result(ctx("shell-idle.txt")) is None


def test_completion_words_in_the_wrong_place_are_not_a_result():
    assert prose_body(["Finished"]) is None
    assert prose_body(["Done"]) is None
    readme = PaneContext(title="", command="codex", lines=(
        "README",
        "Status: Finished",
        "› Done please",
        "› Ask Codex to do anything",
    ))
    assert CodexAdapter().extract_result(readme) is None
    working = PaneContext(title="", command="codex", lines=(
        "The answer was complete.",
        "• Working (2s • esc to interrupt)",
        "tab to queue message",
    ))
    assert CodexAdapter().extract_result(working) is None


def test_same_result_does_not_become_ready_again_after_a_new_turn():
    tracker = ResultTracker()
    codex = CodexAdapter()
    first = codex.extract_result(ctx("codex-idle.txt"))
    assert tracker.observe("%1", "IDLE", first).state == "ready"
    again = tracker.observe("%1", "IDLE", first)
    assert again.state == "ready"
    assert again.fingerprint == first.fingerprint
    assert tracker.observe("%1", "WORKING", None).state == "none"
    stale = tracker.observe("%1", "IDLE", first)
    assert stale.state == "none"
    newer = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=(
        "A second answer that is actually new.",
        "Worked for 1m 1s",
        "› Ask Codex to do anything",
    )))
    assert newer.fingerprint != first.fingerprint
    assert tracker.observe("%1", "IDLE", newer).state == "ready"


def test_view_or_copy_marks_read_and_a_mismatch_does_not():
    tracker = ResultTracker()
    found = CodexAdapter().extract_result(ctx("codex-idle.txt"))
    snap = tracker.observe("%1", "IDLE", found)
    assert tracker.mark_read("%1", "nope") is False
    assert tracker.mark_read("%1", snap.fingerprint) is True
    assert tracker.snapshot("%1").state == "read"
    assert tracker.observe("%1", "IDLE", found).state == "read"


def test_status_payload_exposes_state_but_not_result_text():
    class Tower:
        session = "s"

        def load(self):
            return None

        rows = [{
            "key": "%1",
            "host": "MAINPC",
            "project": "demo",
            "agent": "Codex",
            "status": "IDLE",
            "result_state": "ready",
            "result_text": "SECRET ANSWER BODY",
            "remote": False,
            "offline": False,
        }]

    payload = build_status_payload(Tower())
    encoded = json.dumps(payload)
    assert payload["panes"][0]["result_state"] == "ready"
    assert "SECRET ANSWER BODY" not in encoded


def test_phone_page_copies_the_result_not_the_live_pane():
    assert "✓ 새 Result" in PAGE_HTML
    assert "navigator.clipboard" in PAGE_HTML
    assert "결과 복사" in PAGE_HTML
    assert "결과 보기" in PAGE_HTML
