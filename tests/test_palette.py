from types import SimpleNamespace

from tmux_agent_tower.ui import palette


def test_palette_search_combines_commands_and_work_and_cjk():
    tower = SimpleNamespace(rows=[{
        "kind": "pane", "key": "%1", "project": "설정 도구", "agent": "Codex",
    }], visible_rows=[])

    results = palette._items(tower, "설정")

    assert any(category == "command" and key == "settings" for category, key, *_ in results)
    assert any(category == "work" and key == "%1" for category, key, *_ in results)


def test_palette_includes_folder_screen_agent_and_required_commands():
    tower = SimpleNamespace(rows=[
        {"kind": "pane", "key": "%1", "project": "Tower", "agent": "Grok"},
        {"kind": "folder", "key": "folder:1", "folder_name": "실험"},
        {"kind": "window_asset", "key": "window:1", "window_display_name": "빌드"},
    ], visible_rows=[])

    all_items = palette._items(tower, "")
    assert {item[0] for item in all_items} >= {"command", "work", "folder", "window"}
    commands = {item[1] for item in all_items if item[0] == "command"}
    assert {"new_task", "add_ssh", "environments", "live", "saved", "template", "settings"} <= commands
    assert any(item[2] == "Tower" for item in palette._items(tower, "Grok"))


def test_palette_truncates_cjk_rows_in_a_narrow_terminal(monkeypatch):
    from tmux_agent_tower.ui.render import display_width

    class Screen:
        def getmaxyx(self):
            return 8, 18

        def erase(self):
            pass

        def timeout(self, _value):
            pass

        def refresh(self):
            pass

    captured = []
    monkeypatch.setattr(palette, "safe_add", lambda _screen, _y, _x, value, *_a: captured.append(value))
    monkeypatch.setattr(palette, "read_key", lambda _screen: "\x1b")
    tower = SimpleNamespace(rows=[{
        "kind": "pane", "key": "%1", "display_name": "긴 한글 프로젝트 이름",
        "project": "긴 한글 프로젝트 이름", "agent": "Codex",
    }], visible_rows=[])

    palette.open_palette(Screen(), tower)

    result_lines = [line for line in captured if " · " in line]
    assert result_lines
    assert all(display_width(line) <= 17 for line in result_lines)
