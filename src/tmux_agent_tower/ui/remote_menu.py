"""In-TUI control for Tower Remote (the ``M`` key).

Renders menus and result screens only. Starting, stopping, and reading
status all go through ``server.service`` -- this module does not talk to
Tailscale itself and never shows a shell command.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from ..i18n import t
from ..launcher.config import load_config, set_remote_flag
from ..server import service
from .widgets import run_list_picker, safe_add, show_message_screen

MENU_ACTIONS = ("start", "info", "devices", "autostart", "stop", "back")


def format_pairing_code(code: Optional[str]) -> str:
    if code and len(code) == 6 and code.isdigit():
        return f"{code[:3]} {code[3:]}"
    return code or ""


def badge_text(state: str) -> str:
    key = f"remote.badge.{state}"
    rendered = t(key)
    return rendered if rendered != key else t("remote.badge.stopped")


def menu_entries() -> List[Tuple[str, str]]:
    return [
        ("start", t("remote.menu.start")),
        ("info", t("remote.menu.info")),
        ("devices", t("remote.menu.devices")),
        ("autostart", t("remote.menu.autostart")),
        ("stop", t("remote.menu.stop")),
        ("back", t("remote.menu.back")),
    ]


def menu_title(state: str) -> str:
    state_key = f"remote.state.{state}"
    state_text = t(state_key)
    if state_text == state_key:
        state_text = t("remote.state.stopped")
    return f'{t("remote.menu.title")}    {state_text}'


def ready_lines(result: service.StartResult) -> List[str]:
    """Success screen. Deliberately contains no shell command."""

    lines = []
    if result.already_running:
        lines.append(t("remote.ready.already"))
        lines.append("")
    lines.extend([
        t("remote.ready.tailscale"),
        t("remote.ready.https"),
        t("remote.ready.running"),
        "",
        t("remote.ready.url_label"),
        result.url or "-",
        "",
        t("remote.ready.code_label"),
        format_pairing_code(result.pairing_code) or t("remote.pairing.expired"),
        "",
        t("remote.ready.bookmark"),
    ])
    return lines


def failure_lines(reason: str) -> List[str]:
    key = f"remote.error.{reason}" if reason else "remote.error.generic"
    detail = t(key)
    if detail == key:
        detail = t("remote.error.generic")
    return [detail]


def expiry_text(info: dict) -> str:
    if info.get("expired") or not info.get("expires_at"):
        return t("remote.pairing.expired")
    remaining = int(float(info["expires_at"]) - time.time())
    if remaining <= 0:
        return t("remote.pairing.expired")
    return t("remote.pairing.expires_in", minutes=max(1, (remaining + 59) // 60))


def pairing_lines(info: dict) -> List[str]:
    return [
        t("remote.ready.url_label"),
        info.get("url") or "-",
        "",
        t("remote.ready.code_label"),
        format_pairing_code(info.get("code")) or t("remote.pairing.expired"),
        expiry_text(info),
        "",
        t("remote.ready.bookmark"),
    ]


def autostart_entries(enabled: bool) -> List[Tuple[str, str]]:
    return [
        ("on", f'{"[✓]" if enabled else "[ ]"} {t("remote.autostart.on")}'),
        ("off", f'{"[✓]" if not enabled else "[ ]"} {t("remote.autostart.off")}'),
        ("back", t("remote.menu.back")),
    ]


def intro_seen() -> bool:
    try:
        return bool(load_config().get("remote_intro_seen"))
    except Exception:
        return True


def _show(stdscr, title: str, lines: Sequence[str]) -> None:
    show_message_screen(stdscr, title, lines, footer=t("remote.confirm"))


def _start_and_show(stdscr) -> None:
    stdscr.erase()
    safe_add(stdscr, 2, 2, t("remote.starting"))
    stdscr.refresh()
    try:
        result = service.start("tailscale")
    except Exception:
        _show(stdscr, t("remote.failed.title"), failure_lines("generic"))
        return
    if result.ok:
        _show(stdscr, t("remote.ready.title"), ready_lines(result))
    else:
        _show(stdscr, t("remote.failed.title"), failure_lines(result.reason))


def _show_pairing(stdscr) -> None:
    if service.status(force=True).state != "running":
        _show(stdscr, t("remote.pairing.title"), [t("remote.pairing.not_running")])
        return
    while True:
        info = service.pairing_info()
        _show(stdscr, t("remote.pairing.title"), pairing_lines(info))
        pick = run_list_picker(
            stdscr,
            t("remote.pairing.title"),
            [("regen", t("remote.pairing.regenerate")), ("back", t("remote.menu.back"))],
            footer_hint=t("wizard.hint_list"),
        )
        if pick.cancelled or pick.selected_key != "regen":
            return
        updated = service.regenerate_pairing()
        if not updated:
            _show(stdscr, t("remote.pairing.title"), [t("remote.pairing.regen_failed")])


def _device_label(device: dict) -> str:
    label = device.get("label") or t("remote.devices.unnamed")
    paired_at = device.get("paired_at")
    if not paired_at:
        return label
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(paired_at)))
    except Exception:
        return label
    return f"{label}    {stamp}"


def _show_devices(stdscr) -> None:
    while True:
        devices = service.list_devices()
        items: List[Tuple[str, str]] = [(device["id"], _device_label(device)) for device in devices]
        items.append(("revoke_all", t("remote.devices.revoke_all")))
        items.append(("back", t("remote.menu.back")))
        title = t("remote.devices.title")
        if not devices:
            title = f'{title}    {t("remote.devices.empty")}'
        pick = run_list_picker(stdscr, title, items, footer_hint=t("wizard.hint_list"))
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        if pick.selected_key == "revoke_all":
            service.request_revoke_all()
            _show(stdscr, t("remote.devices.title"), [t("remote.devices.revoked")])
            continue
        confirm = run_list_picker(
            stdscr,
            t("remote.devices.revoke_one"),
            [("yes", t("remote.devices.revoke_one")), ("back", t("remote.menu.back"))],
            footer_hint=t("wizard.hint_list"),
        )
        if not confirm.cancelled and confirm.selected_key == "yes":
            service.request_revoke(pick.selected_key)
            _show(stdscr, t("remote.devices.title"), [t("remote.devices.revoked")])


def _show_autostart(stdscr) -> None:
    while True:
        enabled = bool(load_config().get("remote_autostart"))
        pick = run_list_picker(
            stdscr,
            t("remote.autostart.title"),
            autostart_entries(enabled),
            footer_hint=t("wizard.hint_list"),
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        set_remote_flag("autostart", pick.selected_key == "on")


def open_remote_menu(stdscr) -> None:
    """Blocking ``M`` flow. Returns to the main TUI; never stops Remote
    just because the menu was closed.
    """

    if not intro_seen():
        set_remote_flag("intro_seen", True)
        pick = run_list_picker(
            stdscr,
            t("remote.intro.title"),
            [("start", t("remote.intro.start")), ("later", t("remote.intro.later"))],
            footer_hint=t("wizard.hint_list"),
            preamble=[t("remote.intro.body1"), t("remote.intro.body2")],
        )
        if not pick.cancelled and pick.selected_key == "start":
            _start_and_show(stdscr)
        return

    while True:
        try:
            state = service.status(force=True).state
        except Exception:
            state = "error"
        pick = run_list_picker(stdscr, menu_title(state), menu_entries(), footer_hint=t("wizard.hint_list"))
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        action = pick.selected_key
        try:
            if action == "start":
                _start_and_show(stdscr)
            elif action == "info":
                _show_pairing(stdscr)
            elif action == "devices":
                _show_devices(stdscr)
            elif action == "autostart":
                _show_autostart(stdscr)
            elif action == "stop":
                outcome = service.stop()
                text = {
                    "stopped": t("remote.stopped"),
                    "not_ours": t("remote.error.not_ours"),
                }.get(outcome, t("remote.stopped_none"))
                _show(stdscr, t("remote.menu.stop"), [text])
        except Exception:
            _show(stdscr, t("remote.failed.title"), failure_lines("generic"))
