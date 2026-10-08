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
from .widgets import prompt_text, run_list_picker, show_message_screen


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


def open_settings(stdscr, tower=None) -> None:
    while True:
        pick = run_list_picker(
            stdscr,
            t("settings.title"),
            [
                ("copy", t("settings.copy")),
                ("language", t("settings.language")),
                ("agents", t("settings.agents")),
                ("connection", t("settings.connection")),
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
        elif pick.selected_key == "connection":
            connection = run_list_picker(
                stdscr,
                t("settings.connection"),
                [("environments", t("settings.environments")), ("back", t("settings.back"))],
                footer_hint=t("wizard.hint_list"),
            )
            if connection.selected_key == "environments":
                open_environment_profiles(stdscr, tower)
        elif pick.selected_key == "advanced":
            advanced = run_list_picker(
                stdscr,
                t("settings.advanced"),
                [("keys", t("settings.keys")), ("workflow", t("struct.create_workflow")), ("back", t("settings.back"))],
                footer_hint=t("wizard.hint_list"),
            )
            if advanced.selected_key == "keys":
                _keys_screen(stdscr)
            elif advanced.selected_key == "workflow":
                from .tower import STATE_DIR
                from .workflow_presets import open_workflow_presets

                open_workflow_presets(stdscr, tower, STATE_DIR)


def open_environment_profiles(stdscr, tower=None, *, start_add: bool = False) -> None:
    """Manage SSH profiles; removing one never touches its remote machine."""

    from ..state.environments import add_profile, load_profiles, remove_profile, rename_profile, ssh_target_available
    from .tower import REMOTE_HOSTS_FILE, load_remote_hosts

    def refresh_tower() -> None:
        if tower is None:
            return
        tower.remote_hosts = load_remote_hosts()
        registry = getattr(tower, "_host_registry", None)
        if registry is not None:
            registry.peers = list(tower.remote_hosts)
        if hasattr(tower, "load"):
            tower.load()

    def add_one() -> None:
        name = prompt_text(stdscr, t("environment.name_prompt"))
        if name is None:
            return
        target = prompt_text(stdscr, t("environment.target_prompt"))
        if target is None:
            return
        try:
            add_profile(REMOTE_HOSTS_FILE, name, target)
            refresh_tower()
        except (OSError, ValueError) as exc:
            show_message_screen(stdscr, t("environment.add"), [str(exc)])

    if start_add:
        add_one()

    while True:
        profiles = load_profiles(REMOTE_HOSTS_FILE)
        statuses = {row["ssh_target"]: ssh_target_available(row["ssh_target"]) for row in profiles}
        items = [("local", f'{t("environment.local")}  ·  {t("environment.available")}')]
        items.extend(
            (f'profile:{row["ssh_target"]}', f'{row["display_name"]}  ·  '
             f'{t("environment.available" if statuses[row["ssh_target"]] else "environment.offline")}')
            for row in profiles
        )
        items.extend([("add", t("environment.add")), ("back", t("settings.back"))])
        pick = run_list_picker(
            stdscr, t("settings.environments"), items,
            footer_hint=t("wizard.hint_list"),
            preamble=[t("environment.manage_help")],
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "local":
            show_message_screen(stdscr, t("environment.local"), [t("environment.local_fixed")])
            continue
        if pick.selected_key == "add":
            add_one()
            continue

        target = str(pick.selected_key).partition(":")[2]
        profile = next((row for row in profiles if row["ssh_target"] == target), None)
        if profile is None:
            continue
        action = run_list_picker(
            stdscr, profile["display_name"],
            [("test", t("environment.test")), ("rename", t("environment.rename")),
             ("remove", t("environment.remove")), ("back", t("settings.back"))],
            footer_hint=t("wizard.hint_list"),
            preamble=[f'{t("environment.transport")}: SSH',
                      f'{t("environment.target")}: {profile["ssh_target"]}'],
        )
        if action.selected_key == "test":
            ok = ssh_target_available(target)
            show_message_screen(
                stdscr, t("environment.test"),
                [t("environment.available" if ok else "environment.offline_message").format(name=profile["display_name"])],
            )
        elif action.selected_key == "rename":
            name = prompt_text(stdscr, t("environment.rename_prompt"), initial=profile["display_name"])
            if name is not None:
                try:
                    rename_profile(REMOTE_HOSTS_FILE, target, name)
                    refresh_tower()
                except (OSError, ValueError) as exc:
                    show_message_screen(stdscr, t("environment.rename"), [str(exc)])
        elif action.selected_key == "remove":
            confirm = run_list_picker(
                stdscr, t("environment.remove_title"),
                [("remove", t("environment.remove")), ("cancel", t("menu.cancel"))],
                preamble=[profile["display_name"], t("environment.remove_help")],
                footer_hint=t("wizard.hint_list"),
            )
            if confirm.selected_key == "remove":
                try:
                    remove_profile(REMOTE_HOSTS_FILE, target)
                    refresh_tower()
                except (OSError, ValueError) as exc:
                    show_message_screen(stdscr, t("environment.remove"), [str(exc)])


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
