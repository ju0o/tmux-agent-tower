"""Small reusable curses building blocks for the Workspace Launcher wizard.

Kept separate from ``ui/tower.py`` and ``ui/launcher_wizard.py`` so the
wizard's screen-flow code doesn't get tangled up with raw curses cursor
math. Nothing here knows about tmux, agents, or projects.
"""

from __future__ import annotations

import curses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..i18n import t

ESC = 27
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)


def safe_add(stdscr, y, x, text, attr=0):
    """Write text clipped to the screen; never raises on an off-screen
    write (which curses does on the bottom-right cell of some terminals).
    The single implementation used by every screen in this package.
    """

    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    try:
        stdscr.addnstr(y, x, text, max(0, width - x - 1), attr)
    except curses.error:
        pass


@dataclass
class PickResult:
    cancelled: bool = False
    extra: Optional[str] = None
    selected_key: Any = None
    selected_keys: Set[Any] = field(default_factory=set)


def prompt_text(stdscr, label: str, initial: str = "") -> Optional[str]:
    """Blocking single-line text input. Returns None if the user pressed Esc."""

    height, width = stdscr.getmaxyx()
    y = height - 2
    buf = list(initial)

    curses.curs_set(1)
    stdscr.timeout(-1)

    try:
        while True:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            text = label + "".join(buf)
            safe_add(stdscr, y, 0, text, curses.A_BOLD)
            stdscr.move(y, min(width - 1, len(label) + len(buf)))
            stdscr.refresh()

            key = stdscr.getch()

            if key == ESC:
                return None
            if key in ENTER_KEYS:
                return "".join(buf).strip()
            if key in BACKSPACE_KEYS:
                if buf:
                    buf.pop()
                continue
            if 0 <= key < 256:
                ch = chr(key)
                if ch.isprintable():
                    buf.append(ch)
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(200)


def show_message_screen(stdscr, title: str, lines: Sequence[str], footer: Optional[str] = None) -> None:
    """Blocking full-screen message; any key dismisses it."""

    footer = footer if footer is not None else t("wizard.press_any_key")
    stdscr.timeout(-1)
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        safe_add(stdscr, 0, 2, title, curses.A_BOLD)
        for i, line in enumerate(lines):
            safe_add(stdscr, 2 + i, 2, line)
        safe_add(stdscr, height - 1, 2, footer, curses.A_DIM)
        stdscr.refresh()
        stdscr.getch()
    finally:
        stdscr.timeout(200)


def run_list_picker(
    stdscr,
    title: str,
    items: List[Tuple[Any, str]],
    *,
    multi: bool = False,
    searchable: bool = False,
    checked: Optional[Set[Any]] = None,
    extra_keys: Optional[Dict[str, str]] = None,
    footer_hint: str = "",
) -> PickResult:
    """Generic single/multi-select list picker with optional live search.

    ``items`` is ``[(key, label), ...]``. Filtering (when ``searchable``) is
    a case-insensitive substring match against ``label``. ``extra_keys``
    maps a single lowercase character to a tag string; pressing it
    immediately returns a ``PickResult`` with that tag in ``.extra`` (used
    for e.g. "B" = manual path entry, handled by the caller).
    """

    extra_keys = extra_keys or {}
    checked_keys: Set[Any] = set(checked or set())
    search = ""
    selected_index = 0
    # Two modes so a searchable list's letter hotkeys (e.g. "b" for manual
    # path) don't collide with typing "b" into the search box: "/" enters
    # search-editing mode, where every printable key edits the search text
    # instead of being treated as a hotkey, until Enter/Esc leaves it again
    # (the filter itself stays applied).
    editing_search = False

    stdscr.timeout(-1)

    try:
        while True:
            query = search.lower()
            visible = [(k, label) for k, label in items if query in label.lower()] if query else list(items)
            selected_index = max(0, min(selected_index, len(visible) - 1)) if visible else 0

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            safe_add(stdscr, 0, 2, title, curses.A_BOLD)

            row_y = 2
            if searchable:
                cursor = "_" if editing_search else ""
                safe_add(stdscr, row_y, 2, f'{t("wizard.search_label")} {search}{cursor}', curses.A_DIM)
                row_y += 2

            list_top = row_y
            max_rows = max(1, height - list_top - 3)

            top = 0
            if selected_index >= max_rows:
                top = selected_index - max_rows + 1

            if not visible:
                safe_add(stdscr, list_top, 2, t("wizard.no_matches"), curses.A_DIM)

            for offset, (key, label) in enumerate(visible[top : top + max_rows]):
                real_index = top + offset
                y = list_top + offset
                is_selected = real_index == selected_index
                base_attr = curses.A_REVERSE if is_selected else 0

                prefix = "  "
                if multi:
                    prefix = "[x] " if key in checked_keys else "[ ] "

                marker = "> " if is_selected else "  "
                safe_add(stdscr, y, 0, f"{marker}{prefix}{label}", base_attr)

            hint_y = height - 2
            safe_add(stdscr, hint_y, 2, footer_hint, curses.A_DIM)

            stdscr.refresh()
            key_code = stdscr.getch()

            if editing_search:
                if key_code in (ESC, *ENTER_KEYS):
                    editing_search = False
                    continue
                if key_code in BACKSPACE_KEYS:
                    search = search[:-1]
                    continue
                if 0 <= key_code < 256:
                    ch = chr(key_code)
                    if ch.isprintable():
                        search += ch
                        selected_index = 0
                continue

            if key_code == ESC:
                return PickResult(cancelled=True)

            if key_code == curses.KEY_UP or (not searchable and key_code in (ord("k"), ord("K"))):
                selected_index -= 1
                continue

            if key_code == curses.KEY_DOWN or (not searchable and key_code in (ord("j"), ord("J"))):
                selected_index += 1
                continue

            if key_code in ENTER_KEYS:
                if not visible:
                    continue
                current_key = visible[selected_index][0]
                if multi:
                    return PickResult(selected_keys=checked_keys)
                return PickResult(selected_key=current_key)

            if multi and key_code == ord(" "):
                if visible:
                    current_key = visible[selected_index][0]
                    if current_key in checked_keys:
                        checked_keys.discard(current_key)
                    else:
                        checked_keys.add(current_key)
                continue

            if searchable and key_code == ord("/"):
                editing_search = True
                continue

            char = chr(key_code) if 0 <= key_code < 256 else ""

            if char.lower() in extra_keys:
                return PickResult(
                    extra=extra_keys[char.lower()],
                    selected_key=(visible[selected_index][0] if visible else None),
                )
    finally:
        stdscr.timeout(200)
