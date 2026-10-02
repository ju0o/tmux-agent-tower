import json
from pathlib import Path

from tmux_agent_tower.adapters.base import PaneContext, prose_body
from tmux_agent_tower.adapters.claude import ClaudeAdapter
from tmux_agent_tower.adapters.codex import CodexAdapter
from tmux_agent_tower.adapters.cursor import CursorAdapter
from tmux_agent_tower.adapters.grok import GrokAdapter
from tmux_agent_tower.adapters.opencode import OpenCodeAdapter
from tmux_agent_tower.adapters.shell import ShellAdapter
from tmux_agent_tower.adapters import resolve_adapter
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.detection.topology import adapter_for_screen
from tmux_agent_tower.server.httpapi import build_status_payload
from tmux_agent_tower.server.webui import PAGE_HTML
from tmux_agent_tower.ui.tower import CAPTURE_LINES

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


def test_codex_result_drops_the_tool_diff_and_keeps_a_code_block():
    screen = [
        "이해가 안됨. HTML로 보여줘",
        "",
        "• 요청하신 대로 작전판으로 정리하겠습니다.",
        "• Added plan.html (+3 -0)",
        "1 +<!doctype html>",
        "2 +<html>",
        "+ Show details",
        "정리한 시각적 작전판입니다.",
        "```python",
        "print('tower')",
        "```",
        "Worked for 2m 22s",
        "› Ask Codex to do anything",
    ]
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=tuple(screen)))
    assert result is not None
    assert "Added" not in result.text
    assert "<!doctype" not in result.text
    assert "Show details" not in result.text
    assert "이해가 안됨" not in result.text
    assert "작전판으로 정리" in result.text
    assert "print('tower')" in result.text


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
    assert tracker.snapshot("%1").text == ""
    stale = tracker.observe("%1", "IDLE", first)
    assert stale.state == "none"
    assert stale.text == first.text
    assert tracker.snapshot("%1").text == first.text
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
    assert "✓ 새 결과" in PAGE_HTML
    assert "navigator.clipboard" in PAGE_HTML
    assert "결과 복사" in PAGE_HTML
    assert "결과 보기" in PAGE_HTML


def test_ssh_pane_copies_the_agent_on_screen_not_the_ssh_process():
    process = resolve_adapter("ssh", "Codex", "ssh asus", ())
    assert process.name == "Shell"
    assert process.extract_result(ctx("codex-idle.txt")) is None
    screen = adapter_for_screen(process, "Codex", "ui-via-ssh")
    result = screen.extract_result(ctx("codex-idle.txt"))
    assert result is not None
    assert "Summary of what changed" in result.text


def test_result_capture_reaches_the_answer_above_a_long_tool_log():
    prose = "정리한 답이 여기 있습니다."
    tools = [f"• Added file{i}.txt (+1 -0)" for i in range(40)]
    screen = [prose, *tools, "Worked for 1m 1s", "› Ask Codex to do anything"]
    short = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=tuple(screen[-30:])))
    assert short is None or prose not in (short.text if short else "")
    visible = screen[-800:]
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=tuple(visible)))
    assert result is not None
    assert prose in result.text
    assert "Added" not in result.text


def _codex_screen(*body: str) -> tuple:
    return (*body, "› Ask Codex to do anything")


def test_newest_turn_is_the_only_recovered_body():
    screen = _codex_screen(
        "› Done",
        "older answer that must stay in history",
        "Worked for 10s",
        "› next question",
        "한글 최종 답변입니다.",
        "```python",
        "print(1)",
        "```",
        "• Added notes.txt (+1 -0)",
        "12 +<div>",
        "Worked for 3s",
    )
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen))
    assert result is not None
    assert "한글 최종 답변입니다." in result.text
    assert "print(1)" in result.text
    assert "older answer" not in result.text
    assert "Added" not in result.text
    assert "+<div>" not in result.text
    assert result.text.strip() != "Done"


def test_a_working_turn_hides_the_previous_result():
    screen = (
        "older answer that must stay in history",
        "Worked for 10s",
        "› next question",
        "working (esc to interrupt)",
    )
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen)) is None


def test_an_old_working_line_does_not_hide_a_finished_turn():
    screen = _codex_screen(
        "working (esc to interrupt)",
        "› first",
        "older answer that must stay in history",
        "Worked for 10s",
        "› second",
        "second turn answer is this paragraph",
        "Worked for 2s",
    )
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen))
    assert result is not None
    assert result.text == "second turn answer is this paragraph"


def test_approval_widget_and_readme_are_not_a_result():
    approval = (
        "older answer that must stay in history",
        "Worked for 10s",
        "Allow this command to run?",
        "1. Yes",
        "2. No",
    )
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=approval)) is None
    readme = ("# Tower", "This README explains the project in detail.", "Done")
    assert CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=readme)) is None


def test_scrollback_without_a_turn_boundary_is_not_the_whole_history():
    old = [f"older paragraph number {i} stays out of the copy" for i in range(80)]
    screen = _codex_screen(*old, "recent final answer is here", "Worked for 1s")
    result = CodexAdapter().extract_result(PaneContext(title="", command="codex", lines=screen))
    assert result is not None
    assert "recent final answer is here" in result.text
    assert "older paragraph number 0" not in result.text


def test_polling_does_not_read_recovery_history():
    root = Path(__file__).resolve().parents[1]
    tower = (root / "src/tmux_agent_tower/ui/tower.py").read_text(encoding="utf-8")
    assert "CAPTURE_LINES = 30" in tower
    assert "RECOVERY_LINES" not in tower
    assert CAPTURE_LINES == 30


class _RecoverTower:
    def __init__(self, status="IDLE"):
        self.session = "s"
        self.own_pane_id = "%0"
        self.results = ResultTracker()
        self.rows = [{"key": "%9", "status": status, "remote": False}]
        self.captures = []

    def load(self):
        return None


def _patch_history(monkeypatch, tower, history):
    def run_tmux(args, capture=True, timeout=3):
        if args[:2] == ["list-panes", "-s"]:
            return "%9\n"
        return "Codex\x1fssh\n"

    def capture_pane(pane_id, lines=30):
        tower.captures.append(lines)
        return list(history)

    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.run_tmux", run_tmux)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.pane_exists", lambda pane_id: True)
    monkeypatch.setattr("tmux_agent_tower.control.actions.tmux_capture.capture_pane", capture_pane)


def test_empty_tracker_recovers_a_visible_final_and_a_buried_one(monkeypatch):
    from tmux_agent_tower.control.actions import RECOVERY_LINES, get_result

    visible = _codex_screen("visible final answer here", "Worked for 1s")
    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, visible)
    ok, reason, payload = get_result(tower, "%9")
    assert ok and reason == ""
    assert "visible final answer here" in payload["text"]
    assert tower.captures == [RECOVERY_LINES]

    buried = ["noise"] * 40
    buried += ["› question", "buried final answer here", "Worked for 4s", "› Ask Codex to do anything"]
    tower = _RecoverTower()
    _patch_history(monkeypatch, tower, buried)
    ok, _reason, payload = get_result(tower, "%9")
    assert "buried final answer here" in payload["text"]
    assert "noise" not in payload["text"]


def test_working_status_does_not_restore_a_stale_result(monkeypatch):
    from tmux_agent_tower.control.actions import get_result

    history = _codex_screen("stale answer that is still in history", "Worked for 1s")
    tower = _RecoverTower("WORKING")
    _patch_history(monkeypatch, tower, history)
    ok, _reason, payload = get_result(tower, "%9")
    assert ok
    assert payload["text"] == ""
    assert tower.captures == []


def test_recovery_is_bounded_and_keeps_a_result_the_tracker_already_has(monkeypatch):
    from tmux_agent_tower.adapters.base import ResultCandidate
    from tmux_agent_tower.control.actions import RECOVERY_BYTES, RECOVERY_LINES, _bounded_history, get_result

    oldest = "OLDEST_MARKER_TEXT"
    lines = [oldest] + ["x" * 8000 for _ in range(40)]
    kept = _bounded_history(lines)
    assert oldest not in kept
    assert len(kept) <= RECOVERY_LINES
    assert len("\n".join(kept).encode("utf-8")) <= RECOVERY_BYTES

    tower = _RecoverTower()
    tower.results.observe("%9", "IDLE", ResultCandidate("already stored answer", "fp"))
    _patch_history(monkeypatch, tower, _codex_screen("different answer from history", "Worked for 1s"))
    _ok, _reason, payload = get_result(tower, "%9")
    assert payload["text"] == "already stored answer"
    assert tower.captures == []

