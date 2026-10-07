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


def session_display(session: Optional[str]) -> str:
    return session or t("remote.session.unknown")


def session_lines(current_session: str, status: Optional[service.ServiceStatus]) -> List[str]:
    """Preamble for the M menu: which session this Tower watches, and --
    only when it differs -- which one the running remote is bound to.
    """

    lines = [t("remote.session.label", session=session_display(current_session))]
    if status is not None and status.state in ("running", "error") and status.pid:
        if status.session != current_session:
            lines.append(t("remote.session.remote", session=session_display(status.session)))
    if status is not None and status.state == "error" and status.reason:
        lines.append(failure_lines(status.reason)[0])
    return lines


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
        t("remote.session.label", session=session_display(result.session)),
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
        t("remote.session.label", session=session_display(info.get("session"))),
        "",
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


def rebind_entries() -> List[Tuple[str, str]]:
    return [
        ("switch", t("remote.rebind.switch")),
        ("keep", t("remote.rebind.keep")),
        ("cancel", t("remote.rebind.cancel")),
    ]


def _show_result(stdscr, result: service.StartResult) -> None:
    if result.ok:
        _show(stdscr, t("remote.ready.title"), ready_lines(result))
    else:
        _show(stdscr, t("remote.failed.title"), failure_lines(result.reason))


def _offer_rebind(stdscr, tower, result: service.StartResult) -> None:
    """A remote is alive for another session. Ask; never switch on our own."""

    pick = run_list_picker(
        stdscr,
        t("remote.rebind.title"),
        rebind_entries(),
        footer_hint=t("wizard.hint_list"),
        preamble=[
            t("remote.session.remote", session=session_display(result.running_session)),
            t("remote.session.current", session=session_display(tower.session)),
        ],
    )
    if pick.cancelled or pick.selected_key in (None, "cancel"):
        return
    if pick.selected_key == "keep":
        _show_pairing(stdscr)
        return
    stdscr.erase()
    safe_add(stdscr, 2, 2, t("remote.starting"))
    stdscr.refresh()
    _show_result(stdscr, service.rebind("tailscale", session=tower.session, own_pane_id=tower.own_pane_id))


def _start_and_show(stdscr, tower) -> None:
    stdscr.erase()
    safe_add(stdscr, 2, 2, t("remote.starting"))
    stdscr.refresh()
    try:
        result = service.start("tailscale", session=tower.session, own_pane_id=tower.own_pane_id)
    except Exception:
        _show(stdscr, t("remote.failed.title"), failure_lines("generic"))
        return
    if not result.ok and result.reason == "session_mismatch":
        _offer_rebind(stdscr, tower, result)
        return
    _show_result(stdscr, result)


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


def open_remote_menu(stdscr, tower) -> None:
    """Blocking ``M`` flow for the Tower that pressed it. ``tower.session``
    is what a started remote will watch. Returns to the main TUI; never
    stops Remote just because the menu was closed.
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
            _start_and_show(stdscr, tower)
        return

    while True:
        try:
            status = service.status(force=True)
            state = status.state
        except Exception:
            status = None
            state = "error"
        pick = run_list_picker(
            stdscr,
            menu_title(state),
            menu_entries(),
            footer_hint=t("wizard.hint_list"),
            preamble=session_lines(tower.session, status),
        )
        if pick.cancelled or pick.selected_key in (None, "back"):
            return
        action = pick.selected_key
        try:
            if action == "start":
                _start_and_show(stdscr, tower)
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
