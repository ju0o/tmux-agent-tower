"""In-Tower pane control. Enter stays here; G is the only focus escape.

Live text, prompts, identity, focus, result, and close all call
``control.actions``. This module does not talk to tmux itself.
"""

from __future__ import annotations

import curses
import time

from ..control.actions import (
    close_choices,
    close_pane,
    close_warning,
    focus_pane,
    get_pane_screen,
    get_result,
    respond_attention,
    send_prompt,
)
from ..i18n import t
from . import render
from .widgets import is_enter, is_escape, matches_letter, prompt_text, read_key, run_list_picker, safe_add

# Inside the 0.5–1s band. Recent screen only; the list stays underneath
# in the process, and the client is not moved.
CONTROL_REFRESH_SECONDS = 0.7


def open_control_view(stdscr, tower, pane_key: str) -> None:
    """Block on one pane until Esc. Does not select that pane in tmux."""

    notice = ""
    show_result = False
    follow = True
    scroll = 0
    stdscr.timeout(int(CONTROL_REFRESH_SECONDS * 1000))
    try:
        while True:
            tower.load()
            row = next((item for item in tower.rows if item.get("key") == pane_key), None)
            if row is None or row.get("placeholder"):
                tower.notice = "stale"
                return
            room = _draw(stdscr, tower, row, notice, show_result, follow, scroll)
            notice = ""
            key = read_key(stdscr)
            if key == -1 or key == curses.KEY_RESIZE:
                continue
            if key in (curses.KEY_UP, curses.KEY_PPAGE):
                follow = False
                scroll += room if key == curses.KEY_PPAGE else 1
                continue
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                step = room if key == curses.KEY_NPAGE else 1
                scroll = max(0, scroll - step)
                follow = scroll == 0
                continue
            if key == curses.KEY_END:
                follow = True
                scroll = 0
                continue
            if is_escape(key) or matches_letter(key, "q"):
                return
            if matches_letter(key, "a"):
                notice = _attention_action(tower, row, "approve")
                continue
            if matches_letter(key, "n") and row and row.get("attention") == "approval_required":
                notice = _attention_action(tower, row, "reject")
                continue
            if matches_letter(key, "p"):
                notice = _prompt(stdscr, tower, row)
                show_result = False
                continue
            if matches_letter(key, "y"):
                notice, show_result = _show_result(tower, pane_key)
                continue
            if matches_letter(key, "e"):
                _select(tower, pane_key)
                tower.edit_selected(stdscr)
                continue
            if matches_letter(key, "g"):
                notice = _go(tower, row)
                continue
            if matches_letter(key, "x"):
                if _close(stdscr, tower, row):
                    return
                continue
    finally:
        stdscr.timeout(200)


def _select(tower, pane_key: str) -> None:
    for index, row in enumerate(tower.visible_rows):
        if row.get("key") == pane_key:
            tower.selected = index
            return


def _draw(stdscr, tower, row: dict, notice: str, show_result: bool, follow: bool = True, scroll: int = 0) -> int:
    """Paint the control view. Returns how many live lines fit, for paging."""

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    project = row.get("project") or t("project.no_name")
    safe_add(stdscr, 0, 2, f'{t("control.title")}  {project}', curses.A_BOLD)

    symbol, key = render.execution_badge(row.get("status") or "")
    duration = ""
    seconds = row.get("duration_seconds") or 0
    if seconds and tower.config.get("show_status_duration"):
        duration = " · " + render.format_duration(seconds)
    # Execution is already on the line above. Repeating it here crowded
    # the control view with the same mark twice.
    execution = (symbol, key)
    extra = [badge for badge in render.detail_badges(row) if badge != execution]
    badges = "   ".join(f"{mark} {t(name)}" for mark, name in extra)
    safe_add(stdscr, 1, 2, f'{row.get("agent") or "-"}   {symbol} {t(key)}{duration}')
    safe_add(stdscr, 2, 2, badges)
    safe_add(stdscr, 3, 2, f'{t("detail.activity")}: {row.get("activity_text") or "-"}', curses.A_DIM)
    where = (
        f'{t("detail.session")} {row.get("session") or "-"}   '
        f'{t("detail.window")} {row.get("window_index")}: {row.get("window_name") or ""}   '
        f'{t("detail.pane_id")} {row.get("pane_id") or row.get("key") or "-"}'
    )
    safe_add(stdscr, 4, 2, where, curses.A_DIM)
    sense = render.format_identity_sense(
        row.get("agent_source"),
        row.get("project_source"),
        row.get("auto_agent_source"),
        row.get("auto_project_source"),
    )
    safe_add(stdscr, 5, 2, f'{t("detail.sense")}: {sense}', curses.A_DIM)

    live_label_y = 6
    if row.get("attention_prompt"):
        safe_add(stdscr, 6, 2, row.get("attention_prompt") or "", curses.A_BOLD)
        live_label_y = 7

    live_top = live_label_y + 1
    footer_y = height - 1
    result_lines: list = []
    if show_result:
        ok, _reason, payload = get_result(tower, row.get("key"))
        body = (payload.get("text") or "") if ok else ""
        result_lines = (body.split("\n") if body else [t("control.no_result")])[:6]

    live_bottom = footer_y - (len(result_lines) + 1 if result_lines else 0)
    live_mark = t("control.live_follow") if follow else t("control.live_paused")
    safe_add(stdscr, live_label_y, 2, f'{t("control.live")}  {live_mark}', curses.A_BOLD)
    lines = [_fit_live_line(line, width) for line in _live_lines(tower, row)]
    room = max(1, live_bottom - live_top)
    if follow:
        view = lines[-room:]
    else:
        start = max(0, len(lines) - room - scroll)
        view = lines[start : start + room]
    for offset, line in enumerate(view):
        safe_add(stdscr, live_top + offset, 2, line)

    y = live_bottom
    for line in result_lines:
        safe_add(stdscr, y, 2, line)
        y += 1

    if notice:
        safe_add(stdscr, footer_y, 2, notice[: max(0, width - 3)], curses.A_BOLD)
    else:
        _draw_actions(stdscr, footer_y, row)
    stdscr.noutrefresh()
    curses.doupdate()
    return room


def _fit_live_line(line: str, width: int) -> str:
    return render.truncate_to_width(line, max(4, width - 4))


def _draw_actions(stdscr, y: int, row: dict) -> None:
    """Available keys are bright. A key that cannot run is left out."""

    labels = {
        "P": t("control.key_prompt"),
        "A": t("control.key_approve"),
        "N": t("control.key_reject"),
        "Y": t("control.key_result"),
        "E": t("control.key_edit"),
        "G": t("control.key_go"),
        "X": t("control.key_close"),
        "Esc": t("control.key_back"),
    }
    x = 2
    for key_name, available in render.control_actions(row):
        if not available:
            continue
        text = labels[key_name]
        safe_add(stdscr, y, x, text, curses.A_BOLD)
        x += render.display_width(text) + 2


def _result_label(state: str) -> str:
    if state == "ready":
        return t("control.result_ready")
    if state == "read":
        return t("control.result_read")
    return "-"


def _live_lines(tower, row: dict) -> list:
    if row.get("remote"):
        return [t("control.remote_live")]
    if row.get("status") == "DEAD":
        return [t("control.dead_live")]
    ok, reason, payload = get_pane_screen(tower.session, row.get("key") or "", tower.own_pane_id)
    if not ok:
        return [reason or "not_found"]
    return payload.get("lines") or [t("control.empty_live")]


def _attention_label(row: dict) -> str:
    kind = row.get("attention") or "none"
    if kind == "approval_required":
        return t("attention.approval")
    if kind == "input_required":
        return t("attention.input")
    if kind == "error":
        return t("attention.error")
    return ""


def _hint(row: dict) -> str:
    parts = []
    if row.get("attention") == "approval_required" and not row.get("approval_known"):
        parts.append(t("control.approve_unknown"))
    if row.get("approval_known"):
        parts.append(t("control.key_approve"))
    if row.get("reject_known"):
        parts.append(t("control.key_reject"))
    if row.get("attention") == "input_required":
        parts.append(t("control.key_answer"))
    parts.extend([t("control.key_prompt"), t("control.key_result"), "E", "G", "X", "Esc"])
    return "   ".join(parts)


def _attention_action(tower, row: dict, action: str) -> str:
    if row.get("attention") != "approval_required":
        return ""
    ok, reason = respond_attention(tower, row.get("key"), action)
    if reason == "approval_unknown":
        return t("control.approve_unknown")
    if not ok:
        return t("nav.stale") if reason in ("stale", "not_found", "not_approval") else (reason or "")
    return t("control.approved") if action == "approve" else t("control.rejected")


def _prompt(stdscr, tower, row: dict) -> str:
    if row.get("remote"):
        return t("control.remote_unsupported")
    question = row.get("attention_prompt") or ""
    text = prompt_text(
        stdscr,
        t("control.prompt_label"),
        context_lines=[
            f'{row.get("project") or "-"} / {row.get("agent") or "-"}',
            question,
        ],
    )
    if not text:
        return ""
    outcome = send_prompt(tower, row.get("key"), text, row.get("project"), row.get("agent"))
    if not outcome.ok:
        return outcome.reason or t("control.prompt_failed")
    if not outcome.submitted:
        return t("control.prompt_unconfirmed")
    return t("control.prompt_sent")


def _show_result(tower, pane_key: str) -> tuple:
    """Show the extracted body. Clipboard copy is a later slice."""

    ok, reason, payload = get_result(tower, pane_key)
    if not ok:
        return reason or "", False
    if not payload.get("text"):
        return t("control.no_result"), False
    return t("control.result_shown"), True


def _go(tower, row: dict) -> str:
    if row.get("remote") or not row.get("pane_id"):
        return t("control.remote_unsupported")
    ok, reason = focus_pane(tower.session, row["pane_id"], tower.own_pane_id)
    if not ok:
        tower.notice = "stale"
        return t("nav.stale")
    return t("control.focused")


def _close(stdscr, tower, row: dict) -> bool:
    """True when the view should close because the pane is gone."""

    if row.get("pane_id") and row.get("pane_id") == tower.own_pane_id:
        _message(stdscr, t("control.tower_protected"))
        return False
    if row.get("remote") or not str(row.get("key") or "").startswith("%"):
        _message(stdscr, t("control.remote_unsupported"))
        return False
    if row.get("status") == "DEAD":
        tower.load()
        return True

    pick = run_list_picker(
        stdscr,
        t("control.close_title"),
        close_choices(),
        footer_hint=t("wizard.hint_list"),
        preamble=[close_warning(row.get("status") or "")],
    )
    if pick.cancelled or pick.selected_key != "close":
        return False
    ok, reason = close_pane(tower.session, row["pane_id"], tower.own_pane_id)
    if not ok:
        _message(stdscr, t("nav.stale") if reason in ("stale", "not_found") else t("control.tower_protected") if reason == "tower_pane" else reason)
        return False
    tower.load()
    return True


def _message(stdscr, text: str) -> None:
    height, _width = stdscr.getmaxyx()
    safe_add(stdscr, height - 2, 2, text, curses.A_BOLD)
    stdscr.refresh()
    time.sleep(0.6)
