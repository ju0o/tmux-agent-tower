"""Multiline composer shared by the persistent detail and legacy popup.

``prompt_text`` stays single-line. The persistent detail is the normal input
surface; the popup remains only for older callers.

Measured on this machine (tmux 3.6, ESCDELAY=25, keypad on):
``get_wch`` returns one character at a time. ``paste-buffer -p`` and a
terminal that wraps the paste send ``\\x1b [ 2 0 0 ~``, the body, then
``\\x1b [ 2 0 1 ~``. A later Enter is another ``\\n`` after the end marker.
Korean arrives as one character. The composer tells those apart itself.

Without markers the composer still has two facts that are not a wait on
Enter. Input already queued behind a newline means the newline came with
the paste. A newline that follows its previous key at paste speed came
from the same paste; tmux uses the same cadence rule as
``assume-paste-time``. Enter after a human pause submits. Once this
editor has seen a marker, the terminal wraps pastes and cadence is not
consulted.

Control bytes inside a paste are dropped. The words Enter, Esc, and Ctrl+C
are ordinary text. A pasted payload that would exceed the cap is not cut
down and sent; Enter stays in the editor and shows the too-long notice.

``TOWER_PASTE_TRACE=<file>`` appends one event type per key: marker,
newline, char, special, or decision. It never records the body.
"""

from __future__ import annotations

import curses
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from ..control.actions import MAX_PROMPT_CHARS
from ..i18n import t
from .widgets import is_backspace, is_enter, read_key, safe_add

# Matches the Tower Esc delay so a real paste, already in the buffer, is
# read immediately, while a lone Esc still cancels.
_ESC_MS = 30
# Input already waiting behind a newline is a paste burst, not submit.
_BURST_MS = 40
# Keys closer than this came from one paste. Measured on tmux 3.6 through a
# client pty: pasted characters reach ``get_wch`` 1-2ms apart, and the
# newline that ends a pasted line arrives 1-2ms after the line. Two human
# key presses are tens of milliseconds apart.
PASTE_GAP_S = 0.025
# Only after a paste was already recognised without markers. A newline
# this soon after the previous key belongs to the same paste; a terminal
# that hands over the clipboard line by line pauses tens of milliseconds,
# a person pauses longer before pressing Enter. Measured: a lone blank
# line 80ms behind the previous line submitted the first line.
PASTE_SETTLE_S = 0.3
_PASTE_START = "[200~"
_PASTE_END = "[201~"
_TRACE_ENV = "TOWER_PASTE_TRACE"


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _set_bracketed_paste(enabled: bool) -> None:
    """Ask the terminal to wrap pastes. Always paired with a later disable."""

    seq = b"\x1b[?2004h" if enabled else b"\x1b[?2004l"
    try:
        fd = sys.stdout.fileno()
        if not os.isatty(fd):
            return
        os.write(fd, seq)
    except Exception:
        return


@dataclass
class Layout:
    rows: List[str]
    cursor_row: int
    cursor_col: int


def layout_text(text: str, cursor: int, width: int) -> Layout:
    """Wrap ``text`` to ``width`` columns. The cursor stays on a real row.

    Long lines wrap. They are not scrolled sideways. A blank logical line
    stays a blank row. Wide characters use two columns.
    """

    width = max(1, width)
    cursor = max(0, min(len(text), cursor))
    line_no = text[:cursor].count("\n")
    line_col = cursor - (text[:cursor].rfind("\n") + 1)
    rows: List[str] = []
    cursor_row = 0
    cursor_col = 0
    for index, line in enumerate(text.split("\n")):
        wrapped, local_row, local_col = _wrap_line(line, width, line_col if index == line_no else None)
        if index == line_no:
            cursor_row = len(rows) + local_row
            cursor_col = local_col
        rows.extend(wrapped)
    if not rows:
        rows = [""]
    return Layout(rows, cursor_row, cursor_col)


def _wrap_line(line: str, width: int, cursor_col: Optional[int]):
    if line == "":
        return [""], 0, 0
    rows: List[str] = []
    buf: List[str] = []
    used = 0
    local_row = 0
    local_col = 0
    seen = 0
    for ch in line:
        wide = _char_width(ch)
        if buf and used + wide > width:
            rows.append("".join(buf))
            buf = []
            used = 0
        if cursor_col is not None and seen == cursor_col:
            local_row = len(rows)
            local_col = used
        buf.append(ch)
        used += wide
        seen += 1
    if cursor_col is not None and seen == cursor_col:
        local_row = len(rows)
        local_col = used
    rows.append("".join(buf))
    return rows, local_row, local_col


def scroll_to_cursor(cursor_row: int, view_height: int, current: int) -> int:
    """Keep the cursor row inside the visible window."""

    view_height = max(1, view_height)
    if cursor_row < current:
        return cursor_row
    if cursor_row >= current + view_height:
        return cursor_row - view_height + 1
    return current


def _event_name(key) -> str:
    """Event type for the trace. Never the character itself."""

    if key == -1:
        return "timeout"
    if isinstance(key, int):
        return f"special:{key}"
    if key == "\x1b":
        return "esc"
    if key == "\n":
        return "nl"
    if key == "\r":
        return "cr"
    if key == "\t":
        return "tab"
    if len(key) == 1 and ord(key) < 32:
        return "ctrl"
    return "char"


class Composer:
    """Key state for the editor. ``feed`` takes one ``get_wch`` result.

    ``now`` is when the key was read, not when it was fed. The caller may
    have waited for a peek in between.
    """

    def __init__(
        self,
        max_chars: int = MAX_PROMPT_CHARS,
        *,
        clock: Callable[[], float] = time.monotonic,
        trace: Optional[Callable[[str], None]] = None,
    ):
        self.max_chars = max_chars
        self.text = ""
        self.cursor = 0
        self.pasting = False
        # The terminal wrapped a paste in markers at least once.
        self.bracketed = False
        # A newline was kept without markers. The footer explains it.
        self.unbracketed_paste = False
        self.paste_ready = False
        self._paste_cr = False
        self._suppress_lf = False
        self.notice = ""
        self.closed: Optional[str] = None
        self._esc: Optional[str] = None
        self._clock = clock
        self._trace = trace
        self._last_at: Optional[float] = None

    def trace(self, event: str) -> None:
        if self._trace is not None:
            self._trace(event)

    def feed(self, key, *, more: bool = False, now: Optional[float] = None) -> None:
        if self.closed:
            return
        at = self._clock() if now is None else now
        gap = None if self._last_at is None else at - self._last_at
        self._last_at = at
        self.trace(_event_name(key))
        if self._suppress_lf:
            self._suppress_lf = False
            if key == "\n":
                self.trace("crlf_normalized")
                return
        if self._esc is not None:
            self._feed_escape(key)
            return
        if key == "\x1b":
            self._esc = ""
            return
        if self.pasting:
            self._feed_paste(key)
            return
        self._feed_normal(key, more=more, gap=gap)

    def timeout(self) -> None:
        """Nothing followed the pending Esc. A bare Esc cancels."""

        if self.closed or self._esc is None:
            return
        bare = self._esc == "" and not self.pasting
        self._esc = None
        if bare:
            self.trace("cancel")
            self.closed = "cancel"

    def _feed_escape(self, key) -> None:
        if not isinstance(key, str) or len(key) != 1 or ord(key) < 32:
            self._drop_escape()
            return
        self._esc = (self._esc or "") + key
        if self._esc == _PASTE_START:
            self._begin_paste()
            return
        if self._esc == _PASTE_END:
            self._end_paste()
            return
        if _PASTE_START.startswith(self._esc) or _PASTE_END.startswith(self._esc):
            return
        self._drop_escape()

    def _begin_paste(self) -> None:
        self.pasting = True
        self.bracketed = True
        self._paste_cr = False
        self.unbracketed_paste = False
        self._esc = None
        self.trace("paste_start")

    def _end_paste(self) -> None:
        self.pasting = False
        self._esc = None
        self.trace("paste_end")
        self.paste_ready = True
        if len(self.text) > self.max_chars:
            self.notice = "too_long"

    def _drop_escape(self) -> None:
        # An unknown sequence is never a key action, in or out of a paste.
        self._esc = None

    def _feed_paste(self, key) -> None:
        if key == "\x1b":
            self._esc = ""
            return
        if not isinstance(key, str):
            return
        if key == "\n" and self._paste_cr:
            self._paste_cr = False
            self.trace("crlf_normalized")
            return
        if key == "\r":
            self._insert("\n")
            self._paste_cr = True
            return
        self._paste_cr = False
        if key == "\n":
            self._insert("\n")
            return
        if key == "\t" or (key.isprintable() and ord(key) >= 32):
            self._insert(key)

    def _feed_normal(self, key, *, more: bool, gap: Optional[float] = None) -> None:
        if key == "\x0f":  # Ctrl+O is distinct from Enter in supported terminals.
            self._insert("\n")
            return
        if is_enter(key):
            if more:
                self.trace("newline_kept:queued")
                self.paste_ready = True
                self._insert("\n")
                self._suppress_lf = key == "\r"
            elif not self.bracketed and gap is not None and gap <= PASTE_GAP_S:
                # Same paste as the key before it. A terminal that wraps
                # pastes never reaches this branch for a manual Enter.
                self.trace("newline_kept:cadence")
                self.unbracketed_paste = True
                self.paste_ready = True
                self._insert("\n")
                self._suppress_lf = key == "\r"
            elif self.unbracketed_paste and gap is not None and gap <= PASTE_SETTLE_S:
                self.trace("newline_kept:settle")
                self.paste_ready = True
                self._insert("\n")
            else:
                self._submit()
            return
        if is_backspace(key):
            self._backspace()
            return
        if key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
            return
        if key == curses.KEY_RIGHT:
            self.cursor = min(len(self.text), self.cursor + 1)
            return
        if key == curses.KEY_UP:
            self._move_line(-1)
            return
        if key == curses.KEY_DOWN:
            self._move_line(1)
            return
        if key == curses.KEY_RESIZE:
            return
        if isinstance(key, str) and key.isprintable():
            self._insert(key)

    def _insert(self, chunk: str) -> None:
        if not chunk:
            return
        # Keep the whole paste on screen. Submit is what refuses the cap.
        # Cutting here would send a shorter prompt than the user pasted.
        # Appending is the usual paste path. Slicing every character makes
        # a 32,000-character paste quadratic.
        if self.cursor == len(self.text):
            self.text += chunk
        else:
            self.text = self.text[: self.cursor] + chunk + self.text[self.cursor :]
        self.cursor += len(chunk)
        if len(self.text) > self.max_chars:
            self.notice = "too_long"
        else:
            self.notice = ""

    def _backspace(self) -> None:
        if self.cursor <= 0:
            return
        self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
        self.cursor -= 1
        if len(self.text) <= self.max_chars:
            self.notice = ""

    def _submit(self) -> None:
        if len(self.text) > self.max_chars:
            self.notice = "too_long"
            self.trace("submit_refused:too_long")
            return
        self.trace("submit")
        self.closed = "submit"

    def _move_line(self, delta: int) -> None:
        line_no = self.text[: self.cursor].count("\n")
        lines = self.text.split("\n")
        col = self.cursor - (self.text[: self.cursor].rfind("\n") + 1)
        target = line_no + delta
        if target < 0 or target >= len(lines):
            return
        col = min(col, len(lines[target]))
        self.cursor = sum(len(lines[i]) + 1 for i in range(target)) + col


def _pad(text: str, width: int) -> str:
    gap = width - sum(_char_width(ch) for ch in text)
    if gap <= 0:
        return text
    return text + (" " * gap)


def _draw(stdscr, title: str, context_lines: Sequence[str], composer: Composer, scroll: int) -> int:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    safe_add(stdscr, 0, 0, title, curses.A_BOLD)
    row = 1
    for line in context_lines:
        if row >= height - 4:
            break
        safe_add(stdscr, row, 0, line, curses.A_DIM)
        row += 1
    inner = max(1, width - 2)
    view = max(1, height - row - 3)
    laid = layout_text(composer.text, composer.cursor, inner)
    scroll = scroll_to_cursor(laid.cursor_row, view, scroll)
    safe_add(stdscr, row, 0, "┌" + ("─" * inner) + "┐")
    for offset in range(view):
        source = scroll + offset
        body = laid.rows[source] if 0 <= source < len(laid.rows) else ""
        safe_add(stdscr, row + 1 + offset, 0, "│" + _pad(body, inner) + "│")
    bottom = row + 1 + view
    safe_add(stdscr, bottom, 0, "└" + ("─" * inner) + "┘")
    count = t(
        "control.composer_count",
        used=f"{len(composer.text):,}",
        limit=f"{composer.max_chars:,}",
    )
    safe_add(stdscr, min(height - 1, bottom + 1), 0, f'{t("control.composer_footer")}   {count}', curses.A_DIM)
    if composer.notice == "too_long":
        safe_add(
            stdscr,
            min(height - 1, bottom + 2),
            0,
            t("control.composer_too_long", limit=f"{composer.max_chars:,}"),
            curses.A_BOLD,
        )
    elif composer.paste_ready or composer.unbracketed_paste:
        safe_add(stdscr, min(height - 1, bottom + 2), 0, t("control.composer_paste_hint"), curses.A_DIM)
    cursor_y = row + 1 + (laid.cursor_row - scroll)
    cursor_x = min(width - 1, 1 + laid.cursor_col)
    if 0 <= cursor_y < height:
        try:
            stdscr.move(cursor_y, cursor_x)
        except curses.error:
            pass
    stdscr.refresh()
    return scroll


def _trace_writer() -> Optional[Callable[[str], None]]:
    path = os.environ.get(_TRACE_ENV, "")
    if not path:
        return None
    started = time.monotonic()

    def write(event: str) -> None:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{(time.monotonic() - started) * 1000:.1f} {event}\n")
        except OSError:
            pass

    return write


def prompt_composer(
    stdscr,
    title: str,
    context_lines: Optional[Sequence[str]] = None,
    max_chars: int = MAX_PROMPT_CHARS,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[str]:
    """Block until Esc or a real Enter. None means cancel.

    Newlines inside a bracketed paste, a newline with more input already
    waiting, and a newline at paste speed stay in the text. They do not
    submit. Bracketed paste is enabled on entry and disabled on every
    exit: submit, cancel, and exception.
    """

    composer = Composer(max_chars=max_chars, clock=clock, trace=_trace_writer())
    context = list(context_lines or [])
    queued: List = []
    scroll = 0
    composer.trace("open")
    _set_bracketed_paste(True)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    stdscr.timeout(-1)
    try:
        while composer.closed is None:
            scroll = _draw(stdscr, title, context, composer, scroll)
            key = _take(stdscr, queued)
            at = clock()
            if key == -1:
                composer.timeout()
                continue
            _handle(stdscr, queued, composer, key, at)
            # Input that is already waiting is one paste. Drawing the
            # whole buffer per character is what made a long paste feel
            # stuck, so drain the queue first and draw once.
            while composer.closed is None:
                nxt = _take(stdscr, queued, wait_ms=0)
                at = clock()
                if nxt == -1:
                    break
                _handle(stdscr, queued, composer, nxt, at)
        if composer.closed == "submit":
            return composer.text
        return None
    finally:
        composer.trace("close")
        _set_bracketed_paste(False)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.timeout(200)


def _handle(stdscr, queued: List, composer: Composer, key, at: float) -> None:
    more = False
    if composer._esc is None and not composer.pasting and is_enter(key):
        more = _peek_burst(stdscr, queued)
    composer.feed(key, more=more, now=at)
    # A bracketed paste arrives one character at a time up to the end
    # marker. Nothing is drawn until then.
    while composer.pasting and composer.closed is None:
        nxt = _take(stdscr, queued)
        if nxt == -1:
            break
        composer.feed(nxt)
    while composer._esc is not None and composer.closed is None:
        nxt = _take(stdscr, queued, wait_ms=_ESC_MS)
        if nxt == -1:
            composer.timeout()
        else:
            composer.feed(nxt)


def _take(stdscr, queued: List, wait_ms: int = -1):
    if queued:
        return queued.pop(0)
    if wait_ms == -1:
        return read_key(stdscr)
    stdscr.timeout(wait_ms)
    try:
        return read_key(stdscr)
    finally:
        stdscr.timeout(-1)


def _peek_burst(stdscr, queued: List) -> bool:
    stdscr.timeout(_BURST_MS)
    try:
        nxt = stdscr.get_wch()
    except curses.error:
        return False
    finally:
        stdscr.timeout(-1)
    if nxt == -1:
        return False
    queued.append(nxt)
    return True
