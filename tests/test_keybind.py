import json

from tmux_agent_tower import main
from tmux_agent_tower.i18n import en, ko
from tmux_agent_tower.tmux import keybind, registration
from tmux_agent_tower.ui.settings_menu import keys_menu_items, keys_preamble


USER_CONF = """# keep me
set -g mouse on

# >>> tmux-agent-tower v0.1.1 focus binding >>>
unbind-key w
bind-key w run-shell -b 'tower --focus'
# <<< tmux-agent-tower v0.1.1 focus binding <<<
"""


def test_parse_default_w_command():
    line = "bind-key -T prefix w choose-tree -Zw"
    assert keybind.parse_w_command(line) == "choose-tree -Zw"


def test_custom_w_is_kept_and_tower_owned_is_not():
    custom = "bind-key -T prefix w run-shell -b '$HOME/bin/mine'"
    owned = 'bind-key -T prefix w run-shell -b "tower --focus"'
    assert keybind.choose_fallback(custom, "choose-tree -Zw") == (
        "run-shell -b '$HOME/bin/mine'",
        "current",
    )
    assert keybind.choose_fallback(owned, "choose-tree -Zw") == (
        "choose-tree -Zw",
        "tmux-default",
    )


def test_saved_fallback_is_not_replaced_by_our_own_binding():
    saved = "choose-tree -Zw"
    line = "bind-key -T prefix w if-shell 'tower --has-active' 'run-shell -b \"tower --focus\"' 'choose-tree -Zw'"
    assert keybind.choose_fallback(line, "something-else", saved) == (saved, "saved")


def test_install_is_idempotent_and_preserves_the_rest_of_the_file(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    conf.write_text(USER_CONF, encoding="utf-8")
    applied = []

    first = keybind.install_smart_w(
        conf, state,
        list_keys_line='bind-key -T prefix w run-shell -b "tower --focus"',
        tmux_default="choose-tree -Zw",
        apply=applied.append,
    )
    second = keybind.install_smart_w(
        conf, state,
        list_keys_line=applied[0][-1] and "bind-key -T prefix w if-shell 'tower --has-active'",
        tmux_default="choose-tree -Zw",
        apply=applied.append,
    )

    text = conf.read_text(encoding="utf-8")
    assert first == second == "choose-tree -Zw"
    assert text.count(keybind.BEGIN) == 1
    assert "v0.1.1 focus binding" not in text
    assert "set -g mouse on" in text
    assert "# keep me" in text
    assert "tower --has-active" in text
    assert "choose-tree -Zw" in text
    assert applied[0][:4] == ["bind-key", "-T", "prefix", "w"]
    assert applied[0][-1] == "choose-tree -Zw"
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["fallback"] == "choose-tree -Zw"
    assert saved["source"] == "saved"


def test_install_refuses_to_guess_when_no_default_is_known(tmp_path):
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g mouse on\n", encoding="utf-8")
    try:
        keybind.install_smart_w(
            conf, tmp_path / "smart-w.json",
            list_keys_line='bind-key -T prefix w run-shell -b "tower --focus"',
            tmux_default="",
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    assert conf.read_text(encoding="utf-8") == "set -g mouse on\n"
    assert not (tmp_path / "smart-w.json").exists()


def test_restore_removes_only_the_tower_block(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    conf.write_text("set -g mouse on\n\n" + keybind.render_block("choose-tree -Zw"), encoding="utf-8")
    state.write_text(json.dumps({"fallback": "choose-tree -Zw", "source": "tmux-default"}), encoding="utf-8")
    applied = []

    keybind.restore_smart_w(conf, state, apply=applied.append)

    text = conf.read_text(encoding="utf-8")
    assert keybind.BEGIN not in text
    assert "set -g mouse on" in text
    assert applied == [["bind-key", "-T", "prefix", "w", "choose-tree", "-Zw"]]


def test_custom_fallback_round_trips_through_restore(tmp_path):
    conf = tmp_path / ".tmux.conf"
    state = tmp_path / "smart-w.json"
    custom = "bind-key -T prefix w display-message kept"
    keybind.install_smart_w(
        conf, state, list_keys_line=custom, tmux_default="choose-tree -Zw",
    )
    assert "display-message kept" in conf.read_text(encoding="utf-8")
    applied = []
    keybind.restore_smart_w(conf, state, apply=applied.append)
    assert applied == [["bind-key", "-T", "prefix", "w", "display-message", "kept"]]
    assert "display-message kept" not in conf.read_text(encoding="utf-8")


def test_first_backup_is_the_pre_change_file_and_is_not_overwritten(tmp_path):
    conf = tmp_path / ".tmux.conf"
    conf.write_text(USER_CONF, encoding="utf-8")
    keybind.install_smart_w(
        conf, tmp_path / "smart-w.json",
        list_keys_line="bind-key -T prefix w display-menu mine",
        tmux_default="choose-tree -Zw",
    )
    backup = tmp_path / ".tmux.conf.tower-backup"
    assert backup.read_text(encoding="utf-8") == USER_CONF
    keybind.restore_smart_w(conf, tmp_path / "smart-w.json")
    assert backup.read_text(encoding="utf-8") == USER_CONF


def test_has_active_exit_codes(monkeypatch):
    class Fake:
        def __init__(self):
            self.options = {}
            self.alive = {"%7"}

        def current_session(self):
            return "sess"

        def get_session_option(self, session, name):
            return self.options.get((session, name), "")

        def set_session_option(self, session, name, value):
            self.options[(session, name)] = value

        def unset_session_option(self, session, name):
            self.options.pop((session, name), None)

        def pane_exists(self, pane_id):
            return pane_id in self.alive

    fake = Fake()
    monkeypatch.setattr(main.tmux_capture, "current_session", fake.current_session)
    monkeypatch.setattr(registration, "capture", fake)

    assert main.has_active_tower() == 1
    registration.register("sess", "%7")
    assert main.has_active_tower() == 0
    fake.alive.clear()
    assert main.has_active_tower() == 1
    assert fake.get_session_option("sess", registration.PANE_OPTION) == ""


def test_has_active_cli_does_not_start_the_ui(monkeypatch, capsys):
    monkeypatch.setattr(main, "has_active_tower", lambda: 1)
    monkeypatch.setattr(main, "run_ui", lambda: (_ for _ in ()).throw(AssertionError("ui")))
    try:
        main.cli(["--has-active"])
        exited = None
    except SystemExit as exc:
        exited = exc.code
    assert exited == 1
    assert capsys.readouterr().out == ""


def test_keys_install_cli_routes_without_starting_the_ui(monkeypatch, capsys):
    monkeypatch.setattr(main.keybind, "install_for_user", lambda: "choose-tree -Zw")
    monkeypatch.setattr(main, "run_ui", lambda: (_ for _ in ()).throw(AssertionError("ui")))
    try:
        main.cli(["keys", "install"])
        exited = None
    except SystemExit as exc:
        exited = exc.code
    assert exited == 0
    assert "Ctrl+b w" in capsys.readouterr().out


def test_settings_menu_offers_install_or_restore():
    assert keys_menu_items(False)[0][0] == "install"
    assert keys_menu_items(True)[0][0] == "restore"
    on = "\n".join(keys_preamble(True))
    off = "\n".join(keys_preamble(False))
    assert "스마트" in on or "smart" in on.lower()
    assert "창 목록" in off or "window list" in off
    assert ko.STRINGS["hint.settings"].startswith("C ")
    assert en.STRINGS["hint.settings"].startswith("C ")
