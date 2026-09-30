import pytest

from tmux_agent_tower.ui import widgets


@pytest.fixture(autouse=True)
def _no_real_curses(monkeypatch):
    # prompt_text calls curses.curs_set(), which touches the real terminal
    # and requires initscr() to have run -- irrelevant to what these tests
    # check (the screen-clearing/input-buffer logic), so it's stubbed out.
    monkeypatch.setattr(widgets.curses, "curs_set", lambda *_: None)


class FakeStdscr:
    """Minimal fake curses window supporting exactly what prompt_text uses."""

    def __init__(self, keys):
        self._keys = list(keys)
        self.erase_calls = 0
        self.written = []

    def getmaxyx(self):
        return (24, 80)

    def erase(self):
        self.erase_calls += 1

    def addnstr(self, y, x, text, n, attr=0):
        self.written.append((y, x, text))

    def move(self, y, x):
        pass

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def get_wch(self):
        return self._keys.pop(0)


def test_prompt_text_erases_the_screen_each_redraw():
    # Regression: prompt_text used to only clear the single input line via
    # move()+clrtoeol(), leaving whatever screen was open before it (a
    # menu, the main list) visible underneath -- a real cosmetic bug.
    fake = FakeStdscr(list("hi") + ["\n"])
    result = widgets.prompt_text(fake, "Label: ")
    assert result == "hi"
    assert fake.erase_calls >= 1


def test_prompt_text_draws_context_lines_above_input():
    fake = FakeStdscr(["\n"])
    widgets.prompt_text(fake, "Label: ", context_lines=["Current: X", "Auto: Y"])
    texts = [text for _, _, text in fake.written]
    assert "Current: X" in texts
    assert "Auto: Y" in texts


def test_prompt_text_escape_returns_none():
    fake = FakeStdscr(["\x1b"])
    assert widgets.prompt_text(fake, "Label: ") is None


def test_prompt_text_backspace_removes_last_char():
    fake = FakeStdscr(list("ab") + ["\x7f", "\n"])
    assert widgets.prompt_text(fake, "Label: ") == "a"
