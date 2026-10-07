"""Persistent detail behavior after a measured interaction."""

import curses

from tmux_agent_tower.ui import control_view


PAUSE = object()


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.height, self.width = 24, 80
        self.written = []
        self.frames = []

    def getmaxyx(self):
        return self.height, self.width

    def timeout(self, _ms):
        pass

    def get_wch(self):
        try:
            key = next(self.keys)
        except StopIteration:
            raise curses.error("no more keys")
        if key is PAUSE:
            raise curses.error("timeout")
        return key

    def erase(self):
        self.written.clear()

    def addnstr(self, _y, _x, text, _n, _attr=0):
        self.written.append(text)

    def move(self, *_):
        pass

    def noutrefresh(self):
        self.frames.append(tuple(self.written))


class Tower:
    config = {}
    session = "private-test"
    own_pane_id = "%999"

    def __init__(self, row):
        self.rows = [row]
        self.visible_rows = [row]

    def load(self):
        pass


def test_successful_measured_choice_restores_composer_in_same_detail(monkeypatch):
    row = {
        "key": "%1",
        "agent": "Codex",
        "status": "WAITING",
        "attention": "input_required",
        "interaction": {
            "type": "choice",
            "options": [
                {"key": "1", "label": "Use this plan", "safe": True},
                {"key": "2", "label": "Revise the plan", "safe": True},
            ],
        },
    }
    tower = Tower(row)
    screen = Screen(["1", PAUSE, "\x1b"])
    selected = []

    monkeypatch.setattr(
        control_view,
        "get_pane_screen",
        lambda *_: (True, "", {"lines": ["live output"]}),
    )
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "_set_bracketed_paste", lambda *_: None)

    def respond(_tower, _key, key):
        selected.append(key)
        row.update(attention="none", interaction=None)
        return True, ""

    def fail_prompt(*_args):
        raise AssertionError("choice is not a prompt")

    monkeypatch.setattr(control_view, "respond_interaction", respond)
    monkeypatch.setattr(control_view, "send_prompt", fail_prompt)

    control_view.open_control_view(screen, tower, row["key"])

    assert selected == ["1"]
    assert any("선택 필요" in text for text in screen.frames[0])
    assert not any("메시지" in text for text in screen.frames[0])
    assert any(any("메시지" in text for text in frame) for frame in screen.frames[1:])
