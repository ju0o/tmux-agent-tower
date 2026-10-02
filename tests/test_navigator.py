"""Tower as a tmux window/pane navigator.

Enter moves by pane id (select-window + select-pane). A renamed window,
a moved pane, and a stale id never become a window-name lookup or a
keystroke into the pane.
"""

from tmux_agent_tower.tmux import keybind, navigation
from tmux_agent_tower.ui.tower import navigator_rows


def _pane(pane_id, window_index, window_name, pane_index="0", active=False, session="0", window_id=""):
    return {
        "kind": "pane",
        "key": pane_id,
        "session": session,
        "window_id": window_id,
        "window_index": window_index,
        "window_name": window_name,
        "pane_index": pane_index,
        "pane_id": pane_id,
        "pane_active": active,
        "host": "MAINPC",
        "project": pane_id,
        "agent": "Claude",
        "status": "IDLE",
        "remote": False,
    }


def test_navigator_windows_are_selectable_rows():
    rows = navigator_rows(
        [
            _pane("%6", "0", "main", active=True, window_id="@1"),
            _pane("%44", "0", "main", pane_index="1", window_id="@1"),
            _pane("%51", "1", "agents", active=True, window_id="@2"),
            _pane("%52", "1", "agents", pane_index="1", window_id="@2"),
            {"key": "asus:%43", "remote": True, "pane_id": "%43", "host": "ASUS", "session": "0", "window_index": "2", "window_name": "ssh"},
        ]
    )
    assert [row["kind"] for row in rows] == ["window", "pane", "pane", "window", "pane", "pane", "pane"]
    assert [row["key"] for row in rows if row["kind"] == "window"] == ["@1", "@2"]
    assert [row["pane_id"] for row in rows if row["kind"] == "pane"] == ["%6", "%44", "%51", "%52", "%43"]
    assert rows[1]["nav_group"] == rows[2]["nav_group"] == "win:0:@1"
    assert "main" in rows[0]["project"]
    assert rows[-1]["remote"] is True


def test_duplicate_window_names_do_not_merge():
    rows = navigator_rows(
        [
            _pane("%1", "0", "main", window_id="@1"),
            _pane("%2", "1", "main", window_id="@2"),
        ]
    )
    windows = [row for row in rows if row["kind"] == "window"]
    assert [row["key"] for row in windows] == ["@1", "@2"]
    assert windows[0]["nav_group"] != windows[1]["nav_group"]


def test_navigator_visual_draws_a_window_divider_once_per_group(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0", own_pane_id="%6")
    tower.navigator_mode = True
    tower.rows = [
        _pane("%6", "0", "main", window_id="@1"),
        _pane("%44", "0", "main", pane_index="1", window_id="@1"),
        _pane("%51", "1", "agents", window_id="@2"),
    ]
    tower._apply_filter()

    assert [row["kind"] for row in tower.visible_rows] == ["window", "pane", "pane", "window", "pane"]
    assert [row.get("pane_id") for row in tower.visible_rows if row["kind"] == "pane"] == ["%6", "%44", "%51"]
    headers = [(item["host"], item.get("level")) for item in tower.visual if item["type"] == "header"]
    assert headers == [("Session 0", None)]
    tower.selected = len(tower.visible_rows) - 1
    tower.move_down()
    assert tower.selected == 0
    assert tower.visible_rows[0]["kind"] == "window"
    assert tower.control_key() == "@1"


def test_renamed_window_keeps_its_id():
    before = navigator_rows([_pane("%44", "0", "main", window_id="@4")])
    after = navigator_rows([_pane("%44", "0", "renamed", window_id="@4")])
    assert before[0]["kind"] == "window"
    assert before[0]["key"] == after[0]["key"] == "@4"
    assert before[1]["pane_id"] == after[1]["pane_id"] == "%44"
    assert before[0]["nav_group"] == after[0]["nav_group"] == "win:0:@4"
    assert "renamed" in after[0]["project"]


def test_enter_prefers_pane_id_over_window_index(monkeypatch):
    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[:2] == ["list-panes", "-s"]:
            return "%44"
        return ""

    monkeypatch.setattr(navigation.capture, "run_tmux", fake)
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: pane_id == "%44")

    # The row still says window 9. The move must not target 9.
    assert navigation.open_pane("0", "9", "%44") is True
    assert ["select-window", "-t", "%44"] in calls
    assert ["select-pane", "-t", "%44"] in calls
    assert not any("9" in arg for call in calls for arg in call)
    assert not any(call and call[0] == "send-keys" for call in calls)


def test_moved_pane_still_uses_its_id(monkeypatch):
    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[0] == "list-panes" and "-s" in args:
            return "%33"
        return ""

    monkeypatch.setattr(navigation.capture, "run_tmux", fake)
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: True)
    assert navigation.focus_local_pane("0", "%33") is True
    assert calls[1] == ["select-window", "-t", "%33"]
    assert calls[2] == ["select-pane", "-t", "%33"]


def test_stale_pane_does_not_move(monkeypatch):
    calls = []
    monkeypatch.setattr(navigation.capture, "run_tmux", lambda args, capture=True, timeout=3.0: calls.append(list(args)) or "")
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: False)
    assert navigation.focus_local_pane("0", "%999") is False
    assert not any(call[0] in ("select-window", "select-pane", "send-keys") for call in calls)


def test_window_enter_goes_to_that_windows_active_pane(monkeypatch):
    calls = []

    def fake(args, capture=True, timeout=3.0):
        calls.append(list(args))
        if args[0] == "list-panes" and "-s" not in args:
            return "%51\t0\n%52\t1"
        if args[0] == "list-panes":
            return "%51\n%52"
        return ""

    monkeypatch.setattr(navigation.capture, "run_tmux", fake)
    monkeypatch.setattr(navigation.capture, "pane_exists", lambda pane_id: True)
    assert navigation.focus_window("0", "1") is True
    assert ["select-window", "-t", "%52"] in calls
    assert ["select-pane", "-t", "%52"] in calls


def test_gone_window_does_not_move(monkeypatch):
    calls = []
    monkeypatch.setattr(navigation.capture, "run_tmux", lambda args, capture=True, timeout=3.0: calls.append(list(args)) or "")
    assert navigation.focus_window("0", "9") is False
    assert not any(call[0] in ("select-window", "select-pane") for call in calls)


def test_smart_w_binding_does_not_look_up_a_window_name():
    args = keybind.binding_args("choose-tree -Zw", tower_cmd="/opt/tower")
    text = " ".join(args)
    assert "if-shell" in args
    assert "/opt/tower --has-active" in text
    assert "/opt/tower --focus" in text
    assert args[-1] == "choose-tree -Zw"
    assert "window_name" not in text
    assert "choose-tree" in text
