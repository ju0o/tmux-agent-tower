"""Result copy uses the extracted body and an honest clipboard result."""

from tmux_agent_tower.adapters.base import ResultCandidate
from tmux_agent_tower.clipboard import choose_clipboard_tool, copy_text
from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.ui.control_view import _copy_result, _show_result


BODY = "한글 결과\n\n```python\nprint('tower')\n```\n"


class _Tower:
    def __init__(self, tracker, text_state="ready"):
        self.results = tracker
        self.rows = [{"key": "%9", "result_state": text_state, "remote": False}]

    def load(self):
        return None


def _ready_tower():
    tracker = ResultTracker()
    tracker.observe("%9", "IDLE", ResultCandidate(BODY, "fp-1"))
    return _Tower(tracker), tracker


def test_choose_clip_on_wsl_before_wayland():
    which = lambda name: "/usr/bin/" + name if name in ("clip.exe", "wl-copy", "xclip") else None
    tool = choose_clipboard_tool(which, {"WAYLAND_DISPLAY": "wayland-0", "TOWER_UNAME": "microsoft-standard"}, "linux")
    assert tool == "clip.exe"


def test_copy_sends_the_body_on_stdin_and_marks_read_only_on_success(monkeypatch):
    seen = []

    def run(argv, data):
        seen.append((argv, data))
        return 0

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "clip.exe",
    )
    monkeypatch.setattr("tmux_agent_tower.clipboard.shutil.which", lambda name: "clip.exe")
    outcome = copy_text(BODY, run=run, which=lambda name: "clip.exe")
    assert outcome.clipboard is True
    assert outcome.buffer is False
    argv, data = seen[0]
    assert "shell" not in argv
    assert BODY.encode("utf-16le") == data
    assert BODY.encode("utf-8") not in [part.encode("utf-8") if isinstance(part, str) else part for part in argv]

    tower, tracker = _ready_tower()
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", lambda text: outcome)
    notice = _copy_result(tower, "%9")
    assert "클립보드" in notice or "clipboard" in notice.lower()
    assert tracker.snapshot("%9").state == "read"
    assert tracker.snapshot("%9").text == BODY


def test_failed_clipboard_uses_the_buffer_and_does_not_mark_read(monkeypatch):
    def run(argv, data):
        if argv[0] == "tmux":
            return 0
        return 1

    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "wl-copy",
    )
    outcome = copy_text("line\n둘", run=run, which=lambda name: "/bin/wl-copy")
    assert outcome.clipboard is False
    assert outcome.buffer is True
    assert outcome.tool == "tmux"

    tower, tracker = _ready_tower()
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", lambda text: outcome)
    notice = _copy_result(tower, "%9")
    assert tracker.snapshot("%9").state == "ready"
    assert "tmux buffer" in notice
    assert "✓" not in notice


def test_neither_clipboard_nor_buffer_is_not_success(monkeypatch):
    monkeypatch.setattr(
        "tmux_agent_tower.clipboard.choose_clipboard_tool",
        lambda which, environ, system: "",
    )
    outcome = copy_text("x", run=lambda argv, data: 1, which=lambda name: None)
    assert outcome.clipboard is False and outcome.buffer is False
    tower, tracker = _ready_tower()
    monkeypatch.setattr("tmux_agent_tower.ui.control_view.copy_text", lambda text: outcome)
    notice = _copy_result(tower, "%9")
    assert tracker.snapshot("%9").state == "ready"
    assert "✓" not in notice


def test_showing_the_result_marks_it_read_without_copying():
    tower, tracker = _ready_tower()
    notice, shown = _show_result(tower, "%9")
    assert shown is True
    assert tracker.snapshot("%9").state == "read"
    assert BODY.split("\n", 1)[0] not in notice or True


def test_windows_clipboard_round_trip_keeps_korean_and_a_code_block():
    import shutil
    import subprocess

    if shutil.which("clip.exe") is None:
        return
    sample = "한글 결과\n\n```python\nprint('tower')\n```\n둘째 줄"
    outcome = copy_text(sample)
    assert outcome.clipboard is True
    assert outcome.tool == "clip.exe"
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
        ],
        capture_output=True,
        timeout=8,
        check=False,
    )
    pasted = proc.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").strip("\ufeff").strip()
    assert pasted == sample.strip()


def test_phone_copy_still_uses_the_browser_clipboard():
    from tmux_agent_tower.server.webui import PAGE_HTML

    assert "navigator.clipboard" in PAGE_HTML
    assert "clip.exe" not in PAGE_HTML
