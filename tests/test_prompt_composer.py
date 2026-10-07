"""P Prompt keeps pasted newlines. prompt_text stays single-line."""

import curses
import inspect
import time
from collections import deque

import pytest

from tmux_agent_tower.control import actions
from tmux_agent_tower.control.actions import MAX_PROMPT_CHARS
from tmux_agent_tower.server import httpapi, webui
from tmux_agent_tower.ui import control_view
from tmux_agent_tower.ui import prompt_composer as pc
from tmux_agent_tower.ui.prompt_composer import Composer, layout_text, prompt_composer, scroll_to_cursor

# Captured before the autouse fixture replaces it.
REAL_SET_BRACKETED_PASTE = pc._set_bracketed_paste

SAMPLE = (
    "첫째 줄입니다.\n"
    "\n"
    "- 한국어 두 번째 줄\n"
    "- 세 번째 줄\n"
    "\n"
    "```python\n"
    'print("tower")\n'
    'print("multiline")\n'
    "```\n"
    "마지막 줄입니다.\n"
    "Enter\n"
    "Esc\n"
    "Ctrl+C"
)


def _paste(body: str):
    return list("\x1b[200~") + list(body) + list("\x1b[201~")


class Clock:
    """Monotonic stand-in. ``step`` is the gap between two keys."""

    def __init__(self, step: float):
        self.step = step
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


HUMAN = 0.12
PASTE = 0.001


def _drive(keys, max_chars=MAX_PROMPT_CHARS, step=HUMAN, trace=None) -> Composer:
    composer = Composer(max_chars=max_chars, clock=Clock(step), trace=trace)
    for key in keys:
        composer.feed(key)
    return composer


# The user stops here. Nothing is waiting when the editor drains the queue.
PAUSE = object()


class FakeScreen:
    def __init__(self, keys, height=24, width=80, consume_timed_pause=False, key_delay=0):
        self._keys = deque(keys)
        self.height = height
        self.width = width
        self.written = []
        self.timeouts = []
        self._wait = -1
        self.consume_timed_pause = consume_timed_pause
        self.key_delay = key_delay

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        pass

    def addnstr(self, y, x, text, n, attr=0):
        self.written.append(text)

    def move(self, y, x):
        self.cursor = (y, x)

    def refresh(self):
        pass

    def noutrefresh(self):
        pass

    def timeout(self, ms):
        self.timeouts.append(ms)
        self._wait = ms

    def get_wch(self):
        if not self._keys:
            raise curses.error("timeout")
        if self._keys[0] is PAUSE:
            if self._wait != -1:
                if self.consume_timed_pause:
                    self._keys.popleft()
                raise curses.error("timeout")
            self._keys.popleft()
            if getattr(self, "clock", None) is not None:
                self.clock.now += 1.0
            if not self._keys:
                raise curses.error("timeout")
        key = self._keys.popleft()
        if self.key_delay:
            time.sleep(self.key_delay)
        if key == curses.KEY_RESIZE:
            self.height, self.width = 12, 30
        return key


@pytest.fixture(autouse=True)
def _no_tty_paste_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tmux_agent_tower.ui.prompt_composer._set_bracketed_paste",
        lambda enabled: calls.append(enabled),
    )
    monkeypatch.setattr("tmux_agent_tower.ui.prompt_composer.curses.curs_set", lambda *_: None)
    return calls


def _run(screen, step=HUMAN, **kwargs):
    """The screen hands keys over instantly; the clock says how far apart they were."""

    clock = Clock(step)
    screen.clock = clock
    return prompt_composer(screen, "Prompt", clock=clock, **kwargs)


def test_single_line_enter_submits_once():
    composer = _drive(list("hello") + ["\n"])
    assert composer.closed == "submit"
    assert composer.text == "hello"


def test_multiline_paste_keeps_blank_lines_korean_code_and_words():
    composer = _drive(_paste(SAMPLE))
    assert composer.closed is None
    assert composer.pasting is False
    assert composer.text == SAMPLE
    assert "\n\n" in composer.text
    assert "```python" in composer.text
    assert 'print("tower")' in composer.text
    assert "Enter" in composer.text and "Esc" in composer.text and "Ctrl+C" in composer.text


def test_bracketed_crlf_paste_normalizes_each_line_once():
    body = "첫 줄\r\n둘째 줄\r\n\r\n```py\r\nprint('안녕')\r\n```"
    composer = _drive(_paste(body) + ["\n"])
    assert composer.closed == "submit"
    assert composer.text == body.replace("\r\n", "\n")


def test_unbracketed_crlf_paste_normalizes_each_line_once():
    body = "첫 줄\r\n둘째 줄\r\n끝"
    composer = _drive(list(body), step=PASTE)
    assert composer.closed is None
    assert composer.text == body.replace("\r\n", "\n")


def test_ctrl_o_adds_a_manual_newline_and_enter_submits():
    composer = _drive(list("첫 줄") + ["\x0f"] + list("둘째 줄") + ["\n"])
    assert composer.closed == "submit"
    assert composer.text == "첫 줄\n둘째 줄"


def test_first_newline_inside_paste_does_not_submit():
    composer = _drive(list("\x1b[200~") + ["첫", "\n"])
    assert composer.closed is None
    assert composer.pasting is True
    assert composer.text == "첫\n"


def test_paste_end_then_manual_enter_submits_once():
    composer = _drive(_paste(SAMPLE) + ["\n"])
    assert composer.closed == "submit"
    assert composer.text == SAMPLE


def test_burst_newline_is_kept_when_more_input_is_waiting():
    screen = FakeScreen(["\n", "둘", "째", "\n"])
    assert _run(screen) == "\n둘째"


UNBRACKETED = (
    "첫째 줄입니다.\n"
    "\n"
    "둘째 줄입니다.\n"
    "셋째 줄입니다.\n"
    "\n"
    "```python\n"
    'print("tower")\n'
    'print("multiline")\n'
    "```\n"
)


def test_unbracketed_paste_at_paste_speed_keeps_every_line_until_a_human_enter():
    """A terminal without bracketed paste: keys 1ms apart, then Enter later."""

    clock = Clock(PASTE)
    events = []
    composer = Composer(clock=clock, trace=events.append)
    for key in UNBRACKETED:
        composer.feed(key)
    assert composer.closed is None
    assert composer.text == UNBRACKETED
    assert composer.unbracketed_paste is True
    assert "submit" not in events
    assert events.count("newline_kept:cadence") == UNBRACKETED.count("\n")
    clock.now += 0.5
    composer.feed("\n")
    assert composer.closed == "submit"
    assert composer.text == UNBRACKETED


def test_unbracketed_lines_in_separate_writes_keep_their_own_newline():
    """Each line and its newline arrive together; the next line comes 80ms later."""

    clock = Clock(PASTE)
    composer = Composer(clock=clock)
    for line in [piece for piece in UNBRACKETED.split("\n")[:-1] if piece]:
        clock.now += 0.080
        for key in line:
            composer.feed(key)
        composer.feed("\n")
    assert composer.closed is None
    assert composer.text == UNBRACKETED.replace("\n\n", "\n")
    clock.now += 0.5
    composer.feed("\n")
    assert composer.closed == "submit"


def test_a_lone_newline_after_a_pause_with_nothing_queued_submits():
    """No markers, nothing waiting, human gap: that is the user's Enter."""

    clock = Clock(PASTE)
    composer = Composer(clock=clock)
    for key in "첫째 줄입니다.\n":
        composer.feed(key)
    clock.now += 0.5
    composer.feed("\n")
    assert composer.closed == "submit"
    assert composer.text == "첫째 줄입니다.\n"


def test_blank_line_80ms_behind_an_unbracketed_paste_is_still_the_paste():
    """Measured on tmux 3.6: a terminal handing over lines one by one, 80ms apart."""

    clock = Clock(PASTE)
    events = []
    composer = Composer(clock=clock, trace=events.append)
    for key in "첫째 줄입니다.\n":
        composer.feed(key)
    clock.now += 0.080
    composer.feed("\n")
    clock.now += 0.080
    for key in "둘째 줄입니다.\n":
        composer.feed(key)
    assert composer.closed is None
    assert composer.text == "첫째 줄입니다.\n\n둘째 줄입니다.\n"
    assert "newline_kept:settle" in events
    clock.now += 0.5
    composer.feed("\n")
    assert composer.closed == "submit"


def test_settle_window_does_not_apply_before_any_paste_was_seen():
    """Typing a line and pressing Enter 80ms later is a submit."""

    composer = _drive(list("한 줄") + ["\n"], step=0.080)
    assert composer.closed == "submit"


def test_unbracketed_paste_on_screen_shows_the_hint_and_sends_on_one_enter():
    from tmux_agent_tower.i18n import t

    screen = FakeScreen(list(UNBRACKETED) + [PAUSE, "\n"])
    assert _run(screen, step=PASTE) == UNBRACKETED
    hint = t("control.composer_paste_hint")
    assert hint == "붙여넣기 완료 · Enter를 누르면 보냅니다"
    assert any(hint in text for text in screen.written)
    # The pasted body was drawn once after the drain, not once per key.
    assert len(screen.written) < 80


def test_typed_text_then_enter_submits_even_when_typed_fast():
    composer = _drive(list("빠르게 입력") + ["\n"], step=0.030)
    assert composer.closed == "submit"
    assert composer.text == "빠르게 입력"


def test_after_a_bracketed_paste_a_quick_enter_still_submits():
    """Once markers were seen, cadence is not used: Enter outside markers submits."""

    composer = _drive(_paste("한 줄") + ["\n"], step=PASTE)
    assert composer.bracketed is True
    assert composer.closed == "submit"
    assert composer.text == "한 줄"


def test_trace_records_event_types_and_never_the_body():
    events = []
    _drive(_paste("비밀 본문\n") + ["\n"], trace=events.append)
    joined = "\n".join(events)
    assert "paste_start" in events and "paste_end" in events
    assert events[-1] == "submit"
    assert "nl" in events and "char" in events and "esc" in events
    assert "비밀" not in joined and "본문" not in joined


def test_trace_file_is_written_from_the_environment(monkeypatch, tmp_path):
    path = tmp_path / "trace.log"
    monkeypatch.setenv("TOWER_PASTE_TRACE", str(path))
    screen = FakeScreen(list("ab") + ["\n"])
    assert _run(screen) == "ab"
    text = path.read_text()
    assert "open" in text and "submit" in text and "close" in text
    assert "ab" not in text.replace("tab", "")


def test_single_line_prompt_text_is_unchanged():
    """Names, paths, and settings keep the one-line editor. Enter submits at once."""

    from tmux_agent_tower.ui.widgets import prompt_text

    screen = FakeScreen(list("이름") + ["\n", "뒤", "\n"])
    assert prompt_text(screen, "Name: ") == "이름"
    source = inspect.getsource(prompt_text)
    assert "200~" not in source and "bracketed" not in source.lower()


def test_control_bytes_in_paste_are_dropped_and_do_not_act():
    keys = list("\x1b[200~") + ["안", "\x03", "\x1b", "x", "전"] + list("\x1b[201~")
    composer = _drive(keys)
    assert composer.closed is None
    assert composer.text == "안전"
    assert "\x03" not in composer.text
    assert "\x1b" not in composer.text


def test_bare_escape_cancels_and_disables_paste_mode(_no_tty_paste_mode):
    screen = FakeScreen(["가", "\x1b"])
    assert _run(screen) is None
    assert _no_tty_paste_mode == [True, False]


def test_paste_mode_turns_off_after_submit(_no_tty_paste_mode):
    screen = FakeScreen(list("ok") + ["\n"])
    assert _run(screen) == "ok"
    assert _no_tty_paste_mode == [True, False]


def test_paste_mode_turns_off_when_drawing_fails(_no_tty_paste_mode):
    screen = FakeScreen(["\n"])
    screen.getmaxyx = lambda: (_ for _ in ()).throw(RuntimeError("resize"))
    with pytest.raises(RuntimeError):
        _run(screen)
    assert _no_tty_paste_mode == [True, False]


def test_paste_mode_turns_off_when_the_screen_read_raises(_no_tty_paste_mode):
    screen = FakeScreen(["가"])
    screen.get_wch = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run(screen)
    assert _no_tty_paste_mode == [True, False]


def test_bracketed_paste_sequences_are_written_to_the_tty(monkeypatch):
    written = []
    monkeypatch.setattr(pc.os, "isatty", lambda fd: True)
    monkeypatch.setattr(pc.os, "write", lambda fd, data: written.append(data))
    monkeypatch.setattr(pc.sys, "stdout", type("Out", (), {"fileno": staticmethod(lambda: 1)})())
    REAL_SET_BRACKETED_PASTE(True)
    REAL_SET_BRACKETED_PASTE(False)
    assert written == [b"\x1b[?2004h", b"\x1b[?2004l"]


def test_bracketed_paste_sequences_are_not_written_without_a_tty(monkeypatch):
    written = []
    monkeypatch.setattr(pc.os, "isatty", lambda fd: False)
    monkeypatch.setattr(pc.os, "write", lambda fd, data: written.append(data))
    monkeypatch.setattr(pc.sys, "stdout", type("Out", (), {"fileno": staticmethod(lambda: 1)})())
    REAL_SET_BRACKETED_PASTE(True)
    assert written == []


RICH = (
    "# 작업\n"
    "\n"
    "긴 한국어 문장입니다. 빈 줄과 마크다운을 그대로 둡니다.\n"
    "\n"
    "- 첫 항목\n"
    "- 둘째 항목\n"
    "\n"
    "```python\n"
    "print('tower')\n"
    "```\n"
)


def _sized(n: int) -> str:
    if n < len(RICH):
        raise AssertionError("sample longer than the requested size")
    return RICH + ("가" * (n - len(RICH)))


@pytest.mark.parametrize("n", [4001, 10000, 20000, 31999, 32000])
def test_prompt_at_or_under_the_shared_limit_submits_intact(n):
    body = _sized(n)
    composer = _drive(_paste(body) + ["\n"])
    assert composer.closed == "submit"
    assert composer.notice == ""
    assert composer.text == body
    assert len(composer.text) == n
    assert "\n\n" in composer.text
    assert "```python" in composer.text
    assert "긴 한국어" in composer.text


def test_one_character_over_the_limit_is_kept_and_not_sent():
    body = _sized(MAX_PROMPT_CHARS + 1)
    composer = _drive(_paste(body) + ["\n"])
    assert composer.closed is None
    assert composer.notice == "too_long"
    assert composer.text == body
    assert len(composer.text) == MAX_PROMPT_CHARS + 1


def test_backspace_and_resize_keep_the_text():
    screen = FakeScreen(list("가나다") + ["\x7f", curses.KEY_RESIZE, "\n"])
    assert _run(screen) == "가나"
    assert (12, 30) in [screen.getmaxyx()] or screen.width == 30


def test_layout_wraps_wide_hangul_and_keeps_the_cursor_visible():
    text = "가" * 10
    laid = layout_text(text, len(text), 4)
    assert laid.rows == ["가가", "가가", "가가", "가가", "가가"]
    assert laid.cursor_row == 4
    assert scroll_to_cursor(laid.cursor_row, 2, 0) == 3
    blank = layout_text("a\n\nb", 3, 20)
    assert blank.rows[1] == ""


def test_too_long_notice_is_shown_and_nothing_is_returned(_no_tty_paste_mode):
    body = "가" * (MAX_PROMPT_CHARS + 1)
    screen = FakeScreen(_paste(body) + [PAUSE, "\n", PAUSE, "\x1b"])
    started = time.perf_counter()
    assert _run(screen) is None
    elapsed = time.perf_counter() - started
    from tmux_agent_tower.i18n import t

    notice = t("control.composer_too_long", limit=f"{MAX_PROMPT_CHARS:,}")
    count = t(
        "control.composer_count",
        used=f"{MAX_PROMPT_CHARS + 1:,}",
        limit=f"{MAX_PROMPT_CHARS:,}",
    )
    assert notice == "프롬프트가 너무 깁니다. 조금 줄여주세요. (32,000자까지 가능)"
    assert any(notice in text for text in screen.written)
    assert any(count in text for text in screen.written)
    # One draw per key would repaint tens of thousands of times.
    assert len(screen.written) < 200
    assert elapsed < 2.0


def _control_tower(row):
    class Tower:
        rows = [row]
        visible_rows = [row]
        selected = 0
        config = {}
        session = "private-test"
        own_pane_id = "%999"

        def load(self):
            pass

    return Tower()


def test_control_detail_keeps_composer_open_for_immediate_next_prompt(monkeypatch):
    row = {"key": "%1", "pane_id": "%1", "project": "demo", "agent": "Codex", "status": "IDLE", "interaction": {}}
    screen = FakeScreen(list("first\n") + [PAUSE] + list("second\n") + [PAUSE, "\x1b"], consume_timed_pause=True, key_delay=0.03)
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    sent = []
    monkeypatch.setattr(control_view, "send_prompt", lambda _tower, _key, text, *_: sent.append(text) or actions.SubmitResult(True, submitted=True))

    control_view.open_control_view(screen, _control_tower(row), "%1")

    assert sent == ["first", "second"]
    assert any("live output" in text for text in screen.written)
    assert any("메시지" in text for text in screen.written)


def test_persistent_detail_routes_bracketed_paste_through_composer_once(monkeypatch):
    body = "한글 첫 줄\n\n```python\nprint('tower')\n```\n마지막 줄"
    row = {"key": "%1", "pane_id": "%1", "project": "demo", "agent": "Codex", "status": "IDLE", "interaction": {}}
    screen = FakeScreen(_paste(body) + ["\n", PAUSE, "\x1b"], consume_timed_pause=True)
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    paste_mode = []
    monkeypatch.setattr(control_view, "_set_bracketed_paste", paste_mode.append)
    sent = []
    monkeypatch.setattr(control_view, "send_prompt", lambda _tower, _key, text, *_: sent.append(text) or actions.SubmitResult(True, submitted=True))

    control_view.open_control_view(screen, _control_tower(row), "%1")

    assert sent == [body]
    assert paste_mode == [True, False]


def test_persistent_detail_ctrl_o_is_visible_and_sends_manual_multiline(monkeypatch):
    row = {"key": "%1", "pane_id": "%1", "project": "demo", "agent": "Codex", "status": "IDLE", "interaction": {}}
    screen = FakeScreen(list("첫 줄") + ["\x0f"] + list("둘째 줄\n") + [PAUSE, "\x1b"], consume_timed_pause=True, key_delay=0.03)
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    sent = []
    monkeypatch.setattr(control_view, "send_prompt", lambda _tower, _key, text, *_: sent.append(text) or actions.SubmitResult(True, submitted=True))

    control_view.open_control_view(screen, _control_tower(row), "%1")

    from tmux_agent_tower.i18n import t
    assert sent == ["첫 줄\n둘째 줄"]
    assert any(t("control.composer_hint") in text for text in screen.written)


def test_persistent_detail_bare_escape_still_returns(monkeypatch):
    row = {"key": "%1", "pane_id": "%1", "project": "demo", "agent": "Codex", "status": "IDLE", "interaction": {}}
    screen = FakeScreen(["\x1b"])
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_set_bracketed_paste", lambda *_: None)

    control_view.open_control_view(screen, _control_tower(row), "%1")

    assert screen._keys == deque()


def test_uncertain_detail_send_retains_the_draft(monkeypatch):
    row = {"key": "%1", "pane_id": "%1", "project": "demo", "agent": "Codex", "status": "IDLE", "interaction": {}}
    screen = FakeScreen(list("keep this\n") + [PAUSE, "\x1b"], consume_timed_pause=True, key_delay=0.03)
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "send_prompt", lambda *_: actions.SubmitResult(True, submitted=False, reason="submit_not_confirmed"))

    control_view.open_control_view(screen, _control_tower(row), "%1")

    assert any("keep this" in text for text in screen.written)
    assert any("제출 여부를 확인하지 못했습니다" in text for text in screen.written)


def test_detail_drafts_are_restored_per_pane_without_cross_pane_leak(monkeypatch):
    from tmux_agent_tower.ui import control_view

    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    tower = _control_tower({"key": "%1", "pane_id": "%1", "agent": "Codex", "status": "IDLE", "interaction": {}})
    row_a = tower.rows[0]
    screen_a = FakeScreen(list("draft for A") + ["\x1b"])
    control_view.open_control_view(screen_a, tower, "%1")

    row_b = {"key": "%2", "pane_id": "%2", "agent": "Codex", "status": "IDLE", "interaction": {}}
    tower.rows = [row_b]
    screen_b = FakeScreen(["\x1b"])
    control_view.open_control_view(screen_b, tower, "%2")
    assert not any("draft for A" in text for text in screen_b.written)

    tower.rows = [row_a]
    screen_a_again = FakeScreen(["\x1b"])
    control_view.open_control_view(screen_a_again, tower, "%1")
    assert any("draft for A" in text for text in screen_a_again.written)
    assert tower.composer_drafts["%1"] == ("draft for A", len("draft for A"))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"agent": "Codex"}, "메시지"),
        ({"agent": "Shell"}, "명령 입력"),
        ({"agent": "Codex", "transport": "ssh", "auto_agent": "Codex", "auto_agent_source": "process"}, "메시지"),
        ({"agent": "Codex", "transport": "ssh", "auto_agent": "Shell", "auto_agent_source": "process"}, "메시지"),
        ({"transport": "ssh", "auto_agent": "Shell", "auto_agent_source": "process"}, "명령 입력"),
        ({"transport": "ssh", "auto_agent": "Shell", "auto_agent_source": "title"}, "메시지"),
    ],
)
def test_persistent_detail_uses_detected_agent_or_shell_input_label(monkeypatch, row, expected):
    screen = FakeScreen([])
    monkeypatch.setattr(control_view, "get_pane_screen", lambda *_: (True, "", {"lines": ["live output"]}))
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    row = {"key": "%1", "status": "IDLE", "interaction": {}, **row}
    control_view._draw(screen, _control_tower(row), row, "", False,
                       composer=Composer())
    assert any(expected in text for text in screen.written)


def test_send_text_is_still_one_paste_and_not_per_line_keys():
    source = inspect.getsource(actions.send_text)
    assert "load-buffer" in source
    assert "paste-buffer" in source
    assert '["tmux", "send-keys"' not in source


def test_phone_textarea_still_posts_the_raw_value_through_send_prompt():
    page = inspect.getsource(webui)
    assert '<textarea id="prompt-text"' in page
    assert 'maxlength="4000"' not in page
    assert 'maxlength="80"' in page
    assert "text: text" in page
    assert "send_prompt" in inspect.getsource(httpapi.TowerRemoteHandler.do_POST)
    assert f"var PROMPT_LIMIT = {MAX_PROMPT_CHARS};" in webui.PAGE_HTML
    assert inspect.signature(prompt_composer).parameters["max_chars"].default == MAX_PROMPT_CHARS
    assert actions.MAX_IDENTITY_CHARS == 80
