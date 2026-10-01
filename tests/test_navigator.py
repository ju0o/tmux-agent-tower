"""Tower as a tmux window/pane navigator.

Enter moves by pane id (select-window + select-pane). A renamed window,
a moved pane, and a stale id never become a window-name lookup or a
keystroke into the pane.
"""

from tmux_agent_tower.tmux import keybind, navigation
from tmux_agent_tower.ui.tower import navigator_rows


def _pane(pane_id, window_index, window_name, pane_index="0", active=False, session="0"):
    return {
        "kind": "pane",
        "key": pane_id,
        "session": session,
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


def test_navigator_groups_panes_under_windows_without_using_names_as_ids():
    rows = navigator_rows(
        [
            _pane("%6", "0", "main", active=True),
            _pane("%44", "0", "main", pane_index="1"),
            _pane("%51", "1", "agents", active=True),
            _pane("%52", "1", "agents", pane_index="1"),
            {"key": "asus:%43", "remote": True, "pane_id": "%43", "host": "ASUS", "session": "0", "window_index": "2", "window_name": "ssh"},
        ]
    )
    kinds = [(row["kind"], row.get("pane_id") or row["key"]) for row in rows]
    assert kinds == [
        ("window", "win:0:0"),
        ("pane", "%6"),
        ("pane", "%44"),
        ("window", "win:0:1"),
        ("pane", "%51"),
        ("pane", "%52"),
        ("pane", "%43"),
    ]
    assert rows[-1]["remote"] is True


def test_renamed_window_is_only_a_label():
    before = navigator_rows([_pane("%44", "0", "main")])
    after = navigator_rows([_pane("%44", "0", "renamed")])
    assert before[1]["pane_id"] == after[1]["pane_id"] == "%44"
    assert before[1]["key"] == after[1]["key"] == "%44"
    assert after[0]["window_name"] == "renamed"


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
