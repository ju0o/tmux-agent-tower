"""Curses TUI: the actual "Control Tower" screen.

Read-only monitoring plus pane navigation only. No key in this UI sends
keystrokes into a monitored pane, kills anything, or restarts anything --
see docs/ROADMAP.md for the deliberately-not-yet-built "Action Layer".
"""

from __future__ import annotations

import curses
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..adapters import resolve_adapter
from ..adapters.base import PaneContext
from ..detection.status import StatusEngine, STATUS_DEAD
from .. import i18n
from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER
from ..remote.collector import fetch_remote, HOST_STATUS_ONLINE
from ..state.overrides import OverrideStore
from ..state.visits import VisitStore
from ..tmux import capture as tmux_capture
from ..tmux import discovery
from ..tmux import registration
from ..tmux.navigation import open_pane
from .launcher_wizard import run_launcher
from .widgets import is_ctrl_c, is_enter, matches_letter, prompt_text, read_key, run_list_picker, safe_add

REFRESH_SECONDS = 2.0
REMOTE_REFRESH_SECONDS = 6.0
CAPTURE_LINES = 30

STATE_DIR = Path.home() / ".cache" / "tmux-agent-tower"
CONFIG_DIR = Path.home() / ".config" / "tmux-agent-tower"
HOST_FILE = CONFIG_DIR / "host"
REMOTE_HOSTS_FILE = CONFIG_DIR / "remote-hosts.txt"

STATUS_SYMBOL = {
    "WORKING": "●",  # ●
    "WAITING": "!",
    "IDLE": "○",  # ○
    "UNKNOWN": "?",
    "DEAD": "×",  # ×
}

STATUS_ORDER = ["WORKING", "WAITING", "IDLE", "UNKNOWN", "DEAD"]


def local_host_label() -> str:
    try:
        value = HOST_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except Exception:
        pass
    return os.uname().nodename


def load_remote_hosts() -> List[Dict[str, str]]:
    """Each non-comment line: ``alias`` or ``alias:DisplayName``."""

    hosts: List[Dict[str, str]] = []
    try:
        for raw in REMOTE_HOSTS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                alias, name = line.split(":", 1)
            else:
                alias, name = line, line
            hosts.append({"alias": alias.strip(), "name": name.strip().upper()})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return hosts


class Tower:
    def __init__(self, session: str, own_pane_id: str = ""):
        self.session = session
        self.own_pane_id = own_pane_id
        self.local_host = local_host_label()
        self.remote_hosts = load_remote_hosts()

        self.visits = VisitStore(STATE_DIR)
        self.overrides = OverrideStore(STATE_DIR / "overrides.json")
        self.status_engine = StatusEngine()

        self.rows: List[Dict] = []          # selectable data rows only
        self.visual: List[Dict] = []        # rows incl. host headers, for drawing
        self.selected = 0
        self.last_refresh = 0.0
        self._remote_cache: Dict[str, Dict] = {}
        self._remote_last_fetch: Dict[str, float] = {}

    # -- data ---------------------------------------------------------

    def _local_rows(self) -> List[Dict]:
        panes = discovery.list_panes(self.session, self.own_pane_id, CAPTURE_LINES)
        out = []

        for pane in panes:
            adapter = resolve_adapter(pane["command"], pane["title"], pane["cmdline"])
            ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
            status = self.status_engine.evaluate(pane["pane_id"], pane["dead"], adapter, ctx)
            visit = self.visits.visit_label(self.session, pane["pane_id"])
            project = self.overrides.get_project(pane["pane_id"]) or pane["auto_project"]
            agent = self.overrides.get_agent(pane["pane_id"]) or adapter.name

            out.append(
                {
                    "host": self.local_host,
                    "project": project,
                    "agent": agent,
                    "status": status,
                    "visit": visit,
                    "key": pane["pane_id"],
                    "session": pane["session"],
                    "window_index": pane["window_index"],
                    "pane_id": pane["pane_id"],
                    "remote": False,
                }
            )

        return out

    def _remote_rows(self, now: float) -> List[Dict]:
        out = []

        for host_cfg in self.remote_hosts:
            alias, name = host_cfg["alias"], host_cfg["name"]
            last = self._remote_last_fetch.get(alias, 0.0)

            if now - last >= REMOTE_REFRESH_SECONDS or alias not in self._remote_cache:
                self._remote_cache[alias] = fetch_remote(alias, display_name=name)
                self._remote_last_fetch[alias] = now

            snapshot = self._remote_cache[alias]

            if snapshot["status"] != HOST_STATUS_ONLINE:
                out.append(
                    {
                        "host": name,
                        "project": None,
                        "placeholder": "remote.unreachable",
                        "agent": "-",
                        "status": "UNKNOWN",
                        "visit": None,
                        "key": f"remote:{alias}:offline",
                        "remote": True,
                        "offline": True,
                    }
                )
                continue

            if not snapshot["panes"]:
                out.append(
                    {
                        "host": name,
                        "project": None,
                        "placeholder": "remote.no_server",
                        "agent": "-",
                        "status": "IDLE",
                        "visit": None,
                        "key": f"remote:{alias}:empty",
                        "remote": True,
                        "offline": False,
                    }
                )
                continue

            remote_session = f"remote:{alias}"

            for pane in snapshot["panes"]:
                composite_key = f"{alias}:{pane['pane_id']}"
                adapter = resolve_adapter(pane["command"], pane["title"], pane.get("cmdline", ""))
                ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
                status = self.status_engine.evaluate(composite_key, pane["dead"], adapter, ctx)
                visit = self.visits.visit_label(remote_session, pane["pane_id"])
                project = self.overrides.get_project(composite_key) or Path(pane["path"] or "").name or pane["path"]
                agent = self.overrides.get_agent(composite_key) or adapter.name

                out.append(
                    {
                        "host": name,
                        "project": project or "(unknown)",
                        "agent": agent,
                        "status": status,
                        "visit": visit,
                        "key": composite_key,
                        "remote": True,
                        "offline": False,
                    }
                )

        return out

    def load(self) -> None:
        previous_key = None
        if self.rows and 0 <= self.selected < len(self.rows):
            previous_key = self.rows[self.selected]["key"]

        now = time.monotonic()
        rows = self._local_rows() + self._remote_rows(now)
        self.rows = rows

        if not rows:
            self.selected = 0
            return

        if previous_key:
            for idx, row in enumerate(rows):
                if row["key"] == previous_key:
                    self.selected = idx
                    break
            else:
                self.selected = min(self.selected, len(rows) - 1)
        else:
            self.selected = min(self.selected, len(rows) - 1)

        self._build_visual()

    def _build_visual(self) -> None:
        visual = []
        hosts_seen = []
        for row in self.rows:
            if row["host"] not in hosts_seen:
                hosts_seen.append(row["host"])

        for host in hosts_seen:
            visual.append({"type": "header", "host": host})
            for idx, row in enumerate(self.rows):
                if row["host"] == host:
                    visual.append({"type": "data", "row": row, "row_index": idx})

        self.visual = visual

    # -- actions --------------------------------------------------------

    def move_up(self) -> None:
        if not self.rows:
            return
        self.selected = (self.selected - 1) % len(self.rows)

    def move_down(self) -> None:
        if not self.rows:
            return
        self.selected = (self.selected + 1) % len(self.rows)

    def open_selected(self) -> None:
        if not self.rows:
            return
        row = self.rows[self.selected]
        if row.get("remote") or row.get("offline"):
            return
        self.visits.mark_seen(self.session, row["pane_id"])
        open_pane(row["session"], row["window_index"], row["pane_id"])

    def edit_selected(self, stdscr) -> None:
        """The "E" menu: edit this pane's *display* identity only.

        Project name, agent label, and pane title here are all metadata
        Tower shows about a pane -- never a way to control it. The agent
        label in particular can be set to any free text (see
        ``state/overrides.py``'s module docstring): it relabels the row,
        it never changes, restarts, or sends anything to the real process.
        """

        if not self.rows:
            return
        row = self.rows[self.selected]
        if row.get("offline"):
            return

        menu_items = [
            ("project", t("menu.project_name")),
            ("agent", t("menu.agent_name")),
            ("title", t("menu.pane_title")),
            ("reset", t("menu.reset_auto")),
            ("cancel", t("menu.cancel")),
        ]
        pick = run_list_picker(stdscr, t("menu.edit_title"), menu_items, footer_hint=t("wizard.hint_list"))

        if pick.cancelled or pick.selected_key in (None, "cancel"):
            return

        key = row["key"]

        if pick.selected_key == "project":
            name = prompt_text(stdscr, t("prompt.rename"))
            if name:
                self.overrides.set_project(key, name)

        elif pick.selected_key == "agent":
            agent_items = [(label, label) for label in AGENT_LAUNCH_ORDER]
            agent_items.append(("__custom__", t("menu.manual_agent_entry")))
            agent_items.append(("__auto__", t("menu.use_auto_agent")))
            agent_pick = run_list_picker(stdscr, t("menu.agent_name"), agent_items, footer_hint=t("wizard.hint_list"))

            if agent_pick.cancelled or agent_pick.selected_key is None:
                pass
            elif agent_pick.selected_key == "__custom__":
                custom = prompt_text(stdscr, t("prompt.agent_name"))
                if custom:
                    self.overrides.set_agent(key, custom)
            elif agent_pick.selected_key == "__auto__":
                self.overrides.clear_field(key, "agent")
            else:
                self.overrides.set_agent(key, agent_pick.selected_key)

        elif pick.selected_key == "title":
            title = prompt_text(stdscr, t("prompt.pane_title"))
            if title:
                self.overrides.set_title(key, title)
                if not row.get("remote"):
                    # Best-effort: a failed tmux call here never raises
                    # (see tmux/capture.py), so it can't take the rest of
                    # Tower down with it.
                    tmux_capture.run_tmux(["select-pane", "-t", row["pane_id"], "-T", title], capture=False)

        elif pick.selected_key == "reset":
            self.overrides.reset(key)

        self.load()

    def status_counts(self, host: Optional[str] = None) -> Dict[str, int]:
        rows = self.rows if host is None else [r for r in self.rows if r["host"] == host]
        return {name: sum(1 for r in rows if r["status"] == name) for name in STATUS_ORDER}


def setup_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_WHITE, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_RED, -1)
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)


def status_attr(status: str) -> int:
    if not curses.has_colors():
        return 0
    return {
        "WORKING": curses.color_pair(1) | curses.A_BOLD,
        "WAITING": curses.color_pair(2) | curses.A_BOLD,
        "IDLE": curses.color_pair(3) | curses.A_DIM,
        "UNKNOWN": curses.color_pair(4) | curses.A_BOLD,
        "DEAD": curses.color_pair(5) | curses.A_BOLD,
    }.get(status, 0)


def summary_line(tower: Tower) -> str:
    counts = tower.status_counts()
    return "  ".join(f"{STATUS_SYMBOL[s]} {t('status.' + s)} {counts[s]}" for s in STATUS_ORDER)


def host_summary(tower: Tower, host: str) -> str:
    counts = tower.status_counts(host)
    return "  ".join(f"{STATUS_SYMBOL[s]}{counts[s]}" for s in STATUS_ORDER)


def draw(stdscr, tower: Tower) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    safe_add(stdscr, 0, 2, t("app.title"), curses.A_BOLD)
    safe_add(stdscr, 1, 2, summary_line(tower), curses.A_BOLD)
    hint_line = "   ".join(
        [
            t("hint.move"), t("hint.open"), t("hint.rename"),
            t("hint.add_project"), t("hint.new_workspace"),
            t("hint.refresh"), t("hint.quit"),
        ]
    )
    safe_add(stdscr, 2, 2, hint_line, curses.A_DIM)

    project_x = 2
    agent_x = max(30, width - 40)
    status_x = max(44, width - 26)
    visit_x = max(60, width - 10)

    safe_add(stdscr, 3, project_x, t("column.project"), curses.A_BOLD | curses.A_DIM)
    safe_add(stdscr, 3, agent_x, t("column.agent"), curses.A_BOLD | curses.A_DIM)
    safe_add(stdscr, 3, status_x, t("column.status"), curses.A_BOLD | curses.A_DIM)
    safe_add(stdscr, 3, visit_x, t("column.visit"), curses.A_BOLD | curses.A_DIM)

    start_y = 4
    max_rows = max(0, height - start_y - 3)

    if not tower.visual:
        safe_add(stdscr, start_y, 2, t("empty.no_panes"))
        stdscr.noutrefresh()
        curses.doupdate()
        return

    # Ensure the selected data row is inside the visible viewport, counting
    # header rows too (so scrolling still makes sense visually).
    selected_visual_index = 0
    for i, item in enumerate(tower.visual):
        if item["type"] == "data" and item["row_index"] == tower.selected:
            selected_visual_index = i
            break

    top = 0
    if selected_visual_index >= max_rows:
        top = selected_visual_index - max_rows + 1

    visible = tower.visual[top : top + max_rows]

    for offset, item in enumerate(visible):
        y = start_y + offset

        if item["type"] == "header":
            counts_text = host_summary(tower, item["host"])
            safe_add(stdscr, y, 0, f'▼ {item["host"]}  {counts_text}', curses.A_BOLD)
            continue

        row = item["row"]
        is_selected = item["row_index"] == tower.selected

        base_attr = curses.A_REVERSE if is_selected else 0
        if is_selected and curses.has_colors():
            base_attr = curses.color_pair(6) | curses.A_BOLD

        if is_selected:
            safe_add(stdscr, y, 0, " " * max(1, width - 1), base_attr)
            safe_add(stdscr, y, 0, ">", base_attr)

        title_width = max(10, agent_x - project_x - 2)
        text = row["project"] if row["project"] is not None else t(row.get("placeholder", "remote.unreachable"))
        if len(text) > title_width:
            text = text[: max(1, title_width - 1)] + "…"

        safe_add(stdscr, y, project_x, text, base_attr)
        safe_add(stdscr, y, agent_x, row["agent"][:11], base_attr)

        status = row["status"]
        symbol = STATUS_SYMBOL.get(status, "?")
        attr = base_attr if is_selected else status_attr(status)
        safe_add(stdscr, y, status_x, f"{symbol} {t('status.' + status)}", attr)

        visit_text = t("visit." + row["visit"]) if row["visit"] is not None else "-"
        safe_add(stdscr, y, visit_x, visit_text, base_attr)

    footer_y = height - 2
    if tower.rows:
        selected = tower.rows[tower.selected]
        project_text = selected["project"] if selected["project"] is not None else t(selected.get("placeholder", "remote.unreachable"))
        footer = t("footer.selected", host=selected["host"], project=project_text)
        safe_add(stdscr, footer_y, 2, footer, curses.A_BOLD)

    safe_add(
        stdscr,
        height - 1,
        2,
        f'{t("footer.return_hint")}   {t("footer.best_effort")}',
        curses.A_DIM,
    )

    stdscr.noutrefresh()
    curses.doupdate()


def maybe_show_language_picker(stdscr) -> None:
    """Shown exactly once, on the very first run (see
    ``i18n.has_language_configured``). Deliberately bilingual since we
    don't know the answer yet.
    """

    if i18n.has_language_configured():
        return

    pick = run_list_picker(
        stdscr,
        "TMUX AGENT TOWER — 언어를 선택하세요 / Choose your language",
        [("ko", "한국어 (KO)"), ("en", "English (EN)")],
        footer_hint="Enter",
    )

    chosen = pick.selected_key if not pick.cancelled and pick.selected_key else "ko"
    i18n.save_language(chosen)


def main(stdscr, session: Optional[str] = None) -> None:
    setup_colors()
    stdscr.keypad(True)
    stdscr.timeout(200)
    curses.noecho()
    curses.cbreak()

    try:
        curses.curs_set(0)
    except curses.error:
        pass

    maybe_show_language_picker(stdscr)

    session = session or tmux_capture.current_session()
    if not session:
        raise SystemExit(t("cli.no_tmux_session"))

    own_pane_id = tmux_capture.current_pane_id()
    if own_pane_id:
        registration.register(session, own_pane_id)

    try:
        _run_loop(stdscr, session, own_pane_id)
    finally:
        if own_pane_id:
            registration.unregister_if_self(session, own_pane_id)


def _run_loop(stdscr, session: str, own_pane_id: str) -> None:
    tower = Tower(session, own_pane_id)
    tower.load()
    tower.last_refresh = time.monotonic()
    draw(stdscr, tower)

    while True:
        now = time.monotonic()

        if now - tower.last_refresh >= REFRESH_SECONDS:
            tower.load()
            tower.last_refresh = now
            draw(stdscr, tower)

        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            break

        if key == -1:
            continue

        if key == curses.KEY_RESIZE:
            draw(stdscr, tower)
            continue

        if is_ctrl_c(key):
            break

        if key == curses.KEY_UP or matches_letter(key, "k"):
            tower.move_up()
            draw(stdscr, tower)
            continue

        if key == curses.KEY_DOWN or matches_letter(key, "j"):
            tower.move_down()
            draw(stdscr, tower)
            continue

        if is_enter(key):
            tower.open_selected()
            continue

        if matches_letter(key, "e"):
            tower.edit_selected(stdscr)
            draw(stdscr, tower)
            continue

        if matches_letter(key, "r"):
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower)
            continue

        if matches_letter(key, "n"):
            run_launcher(stdscr, tower, multi=False, state_dir=STATE_DIR)
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower)
            continue

        if matches_letter(key, "w"):
            run_launcher(stdscr, tower, multi=True, state_dir=STATE_DIR)
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower)
            continue

        if matches_letter(key, "q"):
            break


def run() -> None:
    # Must happen before curses.wrapper()'s initscr() for ncurses to fully
    # honor the terminal's UTF-8 locale in get_wch() (see widgets.read_key).
    import locale

    locale.setlocale(locale.LC_ALL, "")

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
