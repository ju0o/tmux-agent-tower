"""Tower as a tmux window/pane navigator.

Enter moves by pane id (select-window + select-pane). A renamed window,
a moved pane, and a stale id never become a window-name lookup or a
keystroke into the pane.
"""

from tmux_agent_tower.tmux import keybind, navigation
from tmux_agent_tower.ui.tower import navigator_rows


def _pane(
    pane_id,
    window_index,
    window_name,
    pane_index="0",
    active=False,
    session="0",
    window_id="",
    project="",
    agent="Claude",
    status="IDLE",
    host="MAINPC",
    remote=False,
):
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
        "host": host,
        "project": project or pane_id,
        "agent": agent,
        "status": status,
        "remote": remote,
    }


def test_navigator_windows_are_selectable_rows():
    rows = navigator_rows(
        [
            _pane("%6", "0", "main", active=True, window_id="@1"),
            _pane("%44", "0", "main", pane_index="1", window_id="@1"),
            _pane("%51", "1", "agents", active=True, window_id="@2"),
            _pane("%52", "1", "agents", pane_index="1", window_id="@2"),
            {
                "key": "asus:%43",
                "remote": True,
                "pane_id": "%43",
                "host": "ASUS",
                "session": "0",
                "window_index": "0",
                "window_name": "work",
                "project": "JuAgentEconomy",
                "agent": "Codex",
                "status": "IDLE",
            },
        ]
    )
    assert [row["kind"] for row in rows] == [
        "window", "pane", "pane", "window", "pane", "pane", "window", "pane",
    ]
    assert [row["key"] for row in rows if row["kind"] == "window"] == ["@1", "@2", "ASUS:0"]
    assert [row["pane_id"] for row in rows if row["kind"] == "pane"] == ["%6", "%44", "%51", "%52", "%43"]
    assert rows[1]["nav_group"] == rows[2]["nav_group"] == "win:MAINPC:0:@1"
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
    assert headers == [("MAINPC", None)]
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
    assert before[0]["nav_group"] == after[0]["nav_group"] == "win:MAINPC:0:@4"
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


def test_tree_is_the_default_and_attention_does_not_reorder_it(tmp_path, monkeypatch):
    from tmux_agent_tower.ui import tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0", own_pane_id="%6")
    assert tower.navigator_mode is True
    waiting = _pane("%6", "0", "main", window_id="@1", project="JuPortal", status="IDLE")
    waiting["attention"] = "approval_required"
    idle = _pane("%7", "1", "agents", window_id="@2", project="Other", status="IDLE")
    tower.rows = [waiting, idle]
    tower._apply_filter()
    tree_keys = [row["key"] for row in tower.visible_rows]
    assert tree_keys[0] == "@1"
    assert all(row["kind"] != "window" or row["key"] in ("@1", "@2") for row in tower.visible_rows)

    tower.toggle_attention()
    assert [row["kind"] for row in tower.visible_rows] == ["pane", "pane"]
    assert [row["key"] for row in tower.visible_rows] == ["%6", "%7"]
    tower.toggle_attention()
    assert [row["key"] for row in tower.visible_rows] == tree_keys


def test_collapse_hides_children_and_search_keeps_the_window(tmp_path, monkeypatch):
    from tmux_agent_tower.control.actions import enter_intent
    from tmux_agent_tower.ui import render, tower as tower_module

    monkeypatch.setattr(tower_module, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tower_module, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(tower_module, "HOST_FILE", tmp_path / "config" / "host")
    monkeypatch.setattr(tower_module, "REMOTE_HOSTS_FILE", tmp_path / "config" / "remote-hosts.txt")
    tower = tower_module.Tower("0")
    tower.rows = [
        _pane("%1", "0", "main", window_id="@1", project="Other"),
        _pane("%2", "1", "agents", window_id="@2", project="JuPortal", agent="Cursor"),
        _pane("%3", "1", "agents", pane_index="1", window_id="@2", project="Marketplace", agent="Codex"),
    ]
    tower._apply_filter()
    assert enter_intent(tower.visible_rows[0]) == "window"
    assert tower.control_key() == "@1"
    tower.collapse_selected()
    assert [row["key"] for row in tower.visible_rows if row["kind"] == "pane"] == ["%2", "%3"]
    assert tower.visible_rows[0]["collapsed"] is True
    tower.expand_selected()
    assert "%1" in [row.get("pane_id") for row in tower.visible_rows]

    tower.set_filter("JuPortal")
    shown = tower.visible_rows
    assert [row["key"] for row in shown] == ["@2", "%2"]
    assert shown[0]["host"] == "MAINPC"
    assert "%3" not in [row.get("pane_id") for row in shown]
    tower.set_filter("agents")
    assert {row.get("pane_id") for row in tower.visible_rows if row["kind"] == "pane"} == {"%2", "%3"}

    summary = render.window_summary(
        [
            {"pane_id": "%1", "attention": "approval_required", "status": "IDLE"},
            {"pane_id": "%2", "result_state": "ready", "status": "IDLE"},
            {"pane_id": "%3", "status": "WORKING"},
            {"pane_id": "%4", "status": "IDLE"},
        ]
    )
    assert summary == "!1 ✓1 ●1"
    narrow = render.list_row_parts(
        {"kind": "pane", "project": "쥬포탈", "agent": "Cursor", "status": "WORKING", "pane_id": "%2", "path": "/tmp/secret"},
        58,
        lambda key: "작업 중",
        "3m",
    )
    wide = render.list_row_parts(
        {"kind": "pane", "project": "쥬포탈", "agent": "Cursor", "status": "WORKING", "pane_id": "%2", "guide": "│  ├─ "},
        100,
        lambda key: "작업 중",
        "3m",
    )
    assert narrow["agent"] == ""
    assert "3m" not in narrow["badge"]
    assert "%2" not in "".join(narrow.values())
    assert "/tmp/secret" not in "".join(narrow.values())
    assert wide["agent"] == "Cursor"
    assert "3m" in wide["badge"]
    assert render.display_width("쥬포탈") == 6
    assert render.display_width("├─ ") == render.display_width("└─ ")


def test_live_session_tree_is_readable_and_does_not_focus():
    import subprocess

    from tmux_agent_tower.control.actions import enter_intent

    def tmux(*args):
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)

    if tmux("-V").returncode != 0:
        return
    before = tmux("list-windows", "-t", "0", "-F", "#{window_id}")
    if before.returncode != 0:
        return
    listed = tmux(
        "list-panes",
        "-s",
        "-t",
        "0",
        "-F",
        "#{window_id}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_index}\t#{pane_current_command}",
    )
    panes = []
    for line in listed.stdout.splitlines():
        window_id, index, name, pane_id, pane_index, command = line.split("\t")
        panes.append(
            {
                "kind": "pane",
                "key": pane_id,
                "pane_id": pane_id,
                "session": "0",
                "window_id": window_id,
                "window_index": index,
                "window_name": name,
                "pane_index": pane_index,
                "host": "MAINPC",
                "project": command or pane_id,
                "agent": "Shell",
                "status": "IDLE",
                "remote": False,
            }
        )
    rows = navigator_rows(panes)
    windows = [row for row in rows if row["kind"] == "window"]
    assert windows
    assert all(row["window_id"].startswith("@") for row in windows)
    assert enter_intent(windows[0]) == "window"
    pane = next(row for row in rows if row["kind"] == "pane")
    assert enter_intent(pane) == "control"
    assert pane["guide"]
    after = tmux("list-windows", "-t", "0", "-F", "#{window_id}")
    assert after.stdout == before.stdout
