"""In-Tower settings.

Remote start/stop stays on ``M``. This screen only shows and, on an
explicit choice, installs or removes the smart ``Ctrl+b w`` block.
Starting or quitting Tower does not rebind the key.
"""

from __future__ import annotations

from typing import List, Tuple

from .. import i18n
from ..clipboard_dest import (
    CHOICES,
    LOCAL_HOST,
    TMUX_BUFFER,
    load_preference,
    observe,
    preference_configured,
    resolve_plan,
    run_copy_test,
    save_preference,
    should_offer_copy_setup,
)
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


def copy_menu_items() -> List[Tuple[str, str]]:
    return [
        ("auto", t("copy.auto")),
        ("local_host", t("copy.computer")),
        ("current_terminal", t("copy.terminal")),
        ("tmux_buffer", t("copy.tower")),
        ("test", t("copy.test")),
        ("back", t("settings.back")),
    ]


def open_settings(stdscr) -> None:
    while True:
        pick = run_list_picker(
            stdscr,
            t("settings.title"),
            [
                ("copy", t("settings.copy")),
                ("language", t("settings.language")),
                ("agents", t("settings.agents")),
                ("advanced", t("settings.advanced")),
                ("back", t("settings.back")),
            ],
            footer_hint=t("wizard.hint_list"),
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "copy":
            _copy_screen(stdscr)
        elif pick.selected_key == "language":
            _language_screen(stdscr)
        elif pick.selected_key == "agents":
            _agent_status_screen(stdscr)
        elif pick.selected_key == "advanced":
            advanced = run_list_picker(
                stdscr,
                t("settings.advanced"),
                [("keys", t("settings.keys")), ("back", t("settings.back"))],
                footer_hint=t("wizard.hint_list"),
            )
            if advanced.selected_key == "keys":
                _keys_screen(stdscr)


def _language_screen(stdscr) -> None:
    current = i18n.current_language()
    options = [(current, "한국어" if current == "ko" else "English")]
    other = "en" if current == "ko" else "ko"
    options.append((other, "English" if other == "en" else "한국어"))
    options.append(("back", t("settings.back")))
    pick = run_list_picker(stdscr, t("settings.language"), options, footer_hint=t("wizard.hint_list"))
    if pick.selected_key in ("ko", "en"):
        i18n.save_language(pick.selected_key)


def _agent_status_screen(stdscr) -> None:
    from ..launcher.config import AGENT_LAUNCH_ORDER, load_config, resolve_agent_command

    agents = load_config()["agents"]
    lines = [
        f'{name}  ·  {t("wizard.agent_installed" if resolve_agent_command(name, agents) else "wizard.agent_missing")}'
        for name in AGENT_LAUNCH_ORDER
    ]
    lines.append(t("wizard.agent_login_hint"))
    show_message_screen(stdscr, t("settings.agents"), lines)


def maybe_show_copy_setup(stdscr, *, first_run: bool) -> None:
    """Offer the copy step once, beside the first language question.

    A later launch that already has a language and no destination line
    stays on automatic. Cancelling this step also stays on automatic.
    """

    if not should_offer_copy_setup(first_run, preference_configured()):
        return
    _copy_screen(stdscr)


def _copy_screen(stdscr) -> None:
    while True:
        pick = run_list_picker(
            stdscr,
            t("copy.where"),
            copy_menu_items(),
            footer_hint=t("wizard.hint_list"),
            preamble=[t("copy.setup_step")],
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "test":
            from .tower import local_host_label

            client, clients = observe()
            plan = resolve_plan(load_preference(), client, clients, None)
            manual_destination = None
            if plan.ask or client.ambiguous or not client.tty:
                choice = run_list_picker(
                    stdscr,
                    t("copy.choose_destination"),
                    [
                        (LOCAL_HOST, t("copy.computer")),
                        (TMUX_BUFFER, t("copy.tower")),
                    ],
                    preamble=[t("copy.choose_destination")],
                    footer_hint=t("wizard.hint_list"),
                )
                if choice.cancelled or choice.selected_key not in (LOCAL_HOST, TMUX_BUFFER):
                    continue
                manual_destination = str(choice.selected_key)
            show_message_screen(
                stdscr,
                t("copy.where"),
                [run_copy_test(
                    load_preference(),
                    host_label=local_host_label(),
                    manual_destination=manual_destination,
                    manual_clients=clients if manual_destination else None,
                )],
            )
            continue
        if pick.selected_key in CHOICES:
            save_preference(str(pick.selected_key))
            show_message_screen(stdscr, t("copy.where"), [t("copy.saved_pref")])


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
