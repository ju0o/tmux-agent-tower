import curses
import threading
import time

from tmux_agent_tower.detection.result import ResultTracker
from tmux_agent_tower.ui import control_view, tower as tower_ui, widgets


class _KeyScreen:
    def __init__(self, keys):
        self.keys = iter(keys)

    def get_wch(self):
        try:
            return next(self.keys)
        except StopIteration:
            raise curses.error("no more keys")

    def timeout(self, _value):
        pass


def test_navigation_moves_selection_without_rebuilding_projection(monkeypatch, tmp_path):
    monkeypatch.setattr(tower_ui, "STATE_DIR", tmp_path)
    tower = tower_ui.Tower("test")
    tower.visible_rows = [{"key": "a", "kind": "pane"}, {"key": "b", "kind": "pane"}]
    tower.selected = 0
    monkeypatch.setattr(tower, "_apply_filter", lambda *_: (_ for _ in ()).throw(AssertionError("projection rebuilt")))

    tower.move_down()
    tower.move_up()

    assert tower.selected == 0


def test_home_input_loop_does_not_reload_on_arrow(monkeypatch, tmp_path):
    monkeypatch.setattr(tower_ui, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tower_ui, "REFRESH_SECONDS", 0)
    monkeypatch.setattr(tower_ui, "draw", lambda *_args, **_kwargs: None)
    from tmux_agent_tower.server import service

    monkeypatch.setattr(service, "maybe_autostart", lambda **_kwargs: None)
    monkeypatch.setattr(service, "status", lambda: type("Status", (), {"state": "stopped"})())
    monkeypatch.setattr(service, "on_tui_exit", lambda: None)

    instances = []

    class FakeTower:
        def __init__(self, *_args, **_kwargs):
            self.last_refresh = 0
            self.load_count = 0
            self.refresh_count = 0
            self.rows = []
            self.visible_rows = [{"key": "a", "kind": "pane"}, {"key": "b", "kind": "pane"}]
            self.selected = 0
            instances.append(self)

        def load(self):
            self.load_count += 1

        def start_background_refresh(self):
            self.refresh_count += 1
            return True

        def apply_background_refresh(self):
            return False

        def move_down(self):
            self.selected += 1

    monkeypatch.setattr(tower_ui, "Tower", FakeTower)
    screen = _KeyScreen([curses.KEY_DOWN, "q"])
    tower_ui._run_loop(screen, "session", "")

    assert instances[0].load_count == 1  # initial snapshot only
    assert instances[0].refresh_count >= 1
    assert instances[0].selected == 1


def test_data_refresh_runs_off_input_thread_and_is_adopted_later(monkeypatch, tmp_path):
    monkeypatch.setattr(tower_ui, "STATE_DIR", tmp_path)
    from tmux_agent_tower.server import service

    monkeypatch.setattr(service, "status", lambda: type("Status", (), {"state": "stopped"})())

    class SlowTower(tower_ui.Tower):
        def __init__(self):
            super().__init__("test", results=ResultTracker(tmp_path / "results.sqlite3"))
            self.rows = [{"key": "old", "kind": "pane"}]
            self.visible_rows = list(self.rows)
            self.started = threading.Event()
            self.release = threading.Event()
            self.worker_names = []

        def load(self, *, update_group_labels=True):
            self.worker_names.append(threading.current_thread().name)
            self.started.set()
            self.release.wait(2)
            self.rows = [{"key": "new", "kind": "pane"}]
            self.window_assets = []

        def _apply_filter(self, previous_key=None):
            self.visible_rows = [dict(self.rows[0])]
            self.selected = 0

    tower = SlowTower()
    started_at = time.perf_counter()
    assert tower.start_background_refresh()
    assert (time.perf_counter() - started_at) < 0.1
    assert tower.started.wait(1)
    assert tower.worker_names == ["tower-data-refresh"]
    assert tower.rows[0]["key"] == "old"
    assert not tower.apply_background_refresh()
    assert not tower.start_background_refresh()

    tower.release.set()
    assert tower._refresh_ready.wait(2)
    assert tower.apply_background_refresh()
    assert tower.rows[0]["key"] == "new"
    assert tower.visible_rows[0]["key"] == "new"


def test_first_conversation_frame_uses_cached_row_before_refresh(monkeypatch):
    events = []

    class Tower:
        config = {}
        rows = [{"key": "%1", "kind": "pane", "live_lines": ["cached"]}]
        composer_drafts = {}

        def start_background_refresh(self):
            events.append("refresh-started")
            return True

        def apply_background_refresh(self):
            return False

        def load(self, **_kwargs):
            raise AssertionError("first frame waited for data refresh")

    screen = _KeyScreen(["\x1b"])
    monkeypatch.setattr(control_view, "_draw", lambda *_args: events.append("draw"))
    monkeypatch.setattr(control_view, "_set_bracketed_paste", lambda *_args: None)
    monkeypatch.setattr(control_view, "_set_input_delay", lambda: None)
    monkeypatch.setattr(control_view.curses, "doupdate", lambda: None)
    monkeypatch.setattr(control_view, "event", lambda name, **_fields: events.append(name))
    monkeypatch.setattr(widgets, "event", lambda name, **_fields: events.append(name))

    control_view.open_control_view(screen, Tower(), "%1")

    assert events.index("draw") < events.index("FIRST_DRAW")
    assert events[0] == "refresh-started"


def test_key_decode_reports_categories_without_text(monkeypatch):
    events = []
    monkeypatch.setattr(widgets, "event", lambda name, **fields: events.append((name, fields)))
    screen = _KeyScreen([curses.KEY_DOWN, "j", "\x1b", " ", "secret text"])

    assert [widgets.read_key(screen) for _ in range(5)] == [curses.KEY_DOWN, "j", "\x1b", " ", "secret text"]
    assert [fields["key"] for _name, fields in events] == ["Arrow", "JK", "Esc", "Space", "Other"]
    assert all(set(fields) == {"key"} for _name, fields in events)
