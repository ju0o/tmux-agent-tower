"""In-TUI settings (the ``C`` key).

Remote start/stop stays on ``M``. This screen only shows and, on an
explicit choice, installs or removes the smart ``Ctrl+b w`` block.
Starting or quitting Tower does not rebind the key.
"""

from __future__ import annotations

from typing import List, Tuple

from ..i18n import t
from ..tmux import keybind
from .widgets import run_list_picker, show_message_screen


def keys_menu_items(installed: bool) -> List[Tuple[str, str]]:
    action = ("restore", t("settings.keys.restore")) if installed else ("install", t("settings.keys.install"))
    return [action, ("back", t("settings.back"))]


def keys_preamble(installed: bool) -> List[str]:
    current = t("settings.keys.current_on") if installed else t("settings.keys.current_off")
    return [
        "Ctrl+b → w",
        current,
        "",
        t("settings.keys.explain_on"),
        t("settings.keys.explain_off"),
    ]


def open_settings(stdscr) -> None:
    while True:
        pick = run_list_picker(
            stdscr,
            t("settings.title"),
            [("keys", t("settings.keys")), ("back", t("settings.back"))],
            footer_hint=t("wizard.hint_list"),
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "keys":
            _keys_screen(stdscr)


def _keys_screen(stdscr) -> None:
    while True:
        installed = keybind.installed_for_user()
        pick = run_list_picker(
            stdscr,
            t("settings.keys.title"),
            keys_menu_items(installed),
            footer_hint=t("wizard.hint_list"),
            preamble=keys_preamble(installed),
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "install":
            try:
                keybind.install_for_user()
            except Exception:
                show_message_screen(stdscr, t("settings.keys.title"), [t("settings.keys.failed")])
                continue
            show_message_screen(stdscr, t("settings.keys.title"), [t("settings.keys.installed")])
        elif pick.selected_key == "restore":
            keybind.restore_for_user()
            show_message_screen(stdscr, t("settings.keys.title"), [t("settings.keys.restored")])
