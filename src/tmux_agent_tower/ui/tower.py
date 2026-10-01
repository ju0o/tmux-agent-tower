"""Curses TUI: the actual "Control Tower" screen.

The list monitors panes. Enter opens the in-Tower control view
(``ui/control_view.py``), which is where prompt, edit, focus, and close
happen. ``M`` starts or stops the phone remote. Quitting this TUI leaves
that service running.
"""

from __future__ import annotations

import curses
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..adapters import resolve_adapter
from ..adapters.base import PaneContext
from ..detection.result import ResultTracker
from ..detection.status import StatusEngine, STATUS_DEAD
from .. import i18n
from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER, load_config
from .. import notify
from ..remote.collector import fetch_remote, HOST_STATUS_ONLINE
from ..state.overrides import OverrideStore
from ..state.visits import VisitStore
from ..tmux import capture as tmux_capture
from ..tmux import discovery
from ..tmux import registration
from ..control.actions import enter_intent, update_identity
from ..tmux.navigation import active_pane_in_window
from . import render
from .launcher_wizard import run_launcher
from .widgets import is_backspace, is_ctrl_c, is_enter, is_escape, matches_letter, prompt_text, read_key, run_list_picker, safe_add

REFRESH_SECONDS = 2.0
REMOTE_REFRESH_SECONDS = 6.0
CAPTURE_LINES = 30

STATE_DIR = Path.home() / ".cache" / "tmux-agent-tower"
CONFIG_DIR = Path.home() / ".config" / "tmux-agent-tower"
HOST_FILE = CONFIG_DIR / "host"
REMOTE_HOSTS_FILE = CONFIG_DIR / "remote-hosts.txt"

STATUS_SYMBOL = render.STATUS_SYMBOL
STATUS_ORDER = render.STATUS_ORDER


def _raw_hostname() -> str:
    """The actual OS hostname tmux/a shell would default a pane's title to
    -- deliberately NOT ``local_host_label()``'s (possibly user-overridden)
    display name. A pane titled after the raw hostname carries no real
    information ("just the default"); one that happens to share text with
    a *chosen* display label like "MAINPC" would not mean the same thing.
    """

    return os.uname().nodename


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


def navigator_rows(panes: List[Dict]) -> List[Dict]:
    """Local panes grouped under a selectable window row.

    Remote rows stay at the end. They are visible, and Enter does not
    move the PC to them. Grouping follows session + window index, so a
    renamed window is a label change, not a different target.
    """

    local = [row for row in panes if not row.get("remote") and row.get("pane_id")]
    remote = [row for row in panes if row.get("remote") or not row.get("pane_id")]
    order: List[Tuple] = []
    buckets: Dict[Tuple, List[Dict]] = {}
    for row in local:
        key = (
            str(row.get("session") or ""),
            str(row.get("window_index") if row.get("window_index") is not None else ""),
            str(row.get("window_name") or ""),
        )
        buckets.setdefault(key, []).append(row)
        if key not in order:
            order.append(key)

    out: List[Dict] = []
    for key in order:
        session, index, name = key
        sample = buckets[key][0]
        out.append(
            {
                "kind": "window",
                "key": f"win:{session}:{index}",
                "session": session,
                "window_index": index,
                "window_name": name,
                "host": sample.get("host", ""),
                "project": t("nav.window").format(index=index, name=name),
                "agent": "",
                "status": "",
                "remote": False,
                "pane_id": "",
            }
        )
        for row in buckets[key]:
            item = dict(row)
            item["kind"] = "pane"
            out.append(item)
    for row in remote:
        item = dict(row)
        item["kind"] = "pane"
        out.append(item)
    return out


class Tower:
    def __init__(self, session: str, own_pane_id: str = "", status_engine: Optional[StatusEngine] = None, results: Optional[ResultTracker] = None):
        self.session = session
        self.own_pane_id = own_pane_id
        self.local_host = local_host_label()
        self.remote_hosts = load_remote_hosts()

        self.visits = VisitStore(STATE_DIR)
        self.overrides = OverrideStore(STATE_DIR / "overrides.json")
        # The TUI owns one engine for its lifetime. Remote passes the
        # server's engine in so hysteresis survives across HTTP polls.
        self.status_engine = status_engine or StatusEngine()
        self.results = results

        # Loaded once per Tower run, not re-read every refresh -- matches
        # remote_hosts above. A user editing config.toml mid-session picks
        # it up on the next `tower` restart, not live.
        self.config = load_config()
        self.notifier = notify.NotificationTracker(
            enabled=self.config["notifications"],
            notify_waiting=self.config["notification_kinds"]["waiting"],
            notify_dead=self.config["notification_kinds"]["dead"],
        )

        self.rows: List[Dict] = []           # all selectable data rows (unfiltered)
        self.visible_rows: List[Dict] = []   # rows after the search filter is applied
        self.visual: List[Dict] = []         # visible_rows incl. host headers, for drawing
        self.attention_mode = False          # a separate, re-sorted presentation -- see toggle_attention()
        self.navigator_mode = False          # tmux window/pane tree -- see toggle_navigator()
        self.notice = ""
        self.selected = 0                    # index into visible_rows
        self.last_refresh = 0.0
        self.filter_text = ""
        self._remote_cache: Dict[str, Dict] = {}
        self._remote_last_fetch: Dict[str, float] = {}

    # -- data ---------------------------------------------------------

    def _activity_and_duration(self, key: str, adapter, ctx: PaneContext, status: str, dead: bool):
        """Shared by local/remote row-building: the (possibly None)
        activity text to show, and the current status's duration in
        seconds -- each respecting its own config display toggle.
        """

        activity_text = None
        if self.config["show_activity"] and not dead:
            activity = adapter.extract_activity(ctx)
            # Low-confidence guesses are computed (testable at the adapter
            # level) but never shown -- a plausible-sounding wrong answer
            # is worse than no activity line at all.
            if activity is not None and activity.confidence != "low":
                activity_text = activity.text

        duration_seconds = self.status_engine.duration_seconds(key) if self.config["show_status_duration"] else 0.0

        return activity_text, duration_seconds

    def _result_state(self, key: str, status: str, adapter, ctx: PaneContext, dead: bool) -> str:
        """Uses the capture already taken for status. No second
        ``capture-pane``. Remote rows never call this: their captures
        are title-only and must not be invented into a result.
        """

        if self.results is None:
            return "none"
        candidate = None if dead else adapter.extract_result(ctx)
        return self.results.observe(key, status, candidate).state

    def _local_rows(self) -> List[Dict]:
        panes = discovery.list_panes(self.session, self.own_pane_id, CAPTURE_LINES)
        out = []
        no_name = t("project.no_name")

        for pane in panes:
            adapter = resolve_adapter(pane["command"], pane["title"], pane["cmdline"])
            ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
            status = self.status_engine.evaluate(pane["pane_id"], pane["dead"], adapter, ctx)
            visit = self.visits.visit_label(self.session, pane["pane_id"])

            key = pane["pane_id"]
            custom_project = self.overrides.get_project(key)
            custom_agent = self.overrides.get_agent(key)
            effective_title = pane["title"]  # local title edits are pushed to real tmux -- see edit_selected

            auto_project = render.resolve_display_project(
                None, pane.get("git_project"), effective_title, pane.get("path_basename"), _raw_hostname(), no_name
            )
            project = custom_project or auto_project
            agent = custom_agent or adapter.name
            title_line = render.title_secondary_line(project, effective_title, _raw_hostname())
            activity_text, duration_seconds = self._activity_and_duration(key, adapter, ctx, status, pane["dead"])
            result_state = self._result_state(key, status, adapter, ctx, pane["dead"])
            attention = "none" if pane["dead"] else adapter.detect_attention(ctx)
            attention_prompt = adapter.extract_attention_prompt(ctx) if attention != "none" else ""
            notify_status = "WAITING" if attention in ("approval_required", "input_required") else status

            self.notifier.observe(key, notify_status, project)

            out.append(
                {
                    "host": self.local_host,
                    "project": project,
                    "auto_project": auto_project,
                    "agent": agent,
                    "auto_agent": adapter.name,
                    "title_line": title_line,
                    "activity_text": activity_text,
                    "duration_seconds": duration_seconds,
                    "pane_title": effective_title,
                    "path": pane["path"],
                    "status": status,
                    "attention": attention,
                    "attention_prompt": attention_prompt,
                    "approval_known": bool(attention == "approval_required" and adapter.approve(ctx)),
                    "reject_known": bool(attention == "approval_required" and adapter.reject(ctx)),
                    "result_state": result_state,
                    "visit": visit,
                    "key": key,
                    "session": pane["session"],
                    "window_index": pane["window_index"],
                    "window_name": pane.get("window_name") or "",
                    "pane_index": pane.get("pane_index"),
                    "pane_active": bool(pane.get("pane_active")),
                    "pane_id": pane["pane_id"],
                    "kind": "pane",
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
            no_name = t("project.no_name")

            for pane in snapshot["panes"]:
                composite_key = f"{alias}:{pane['pane_id']}"
                adapter = resolve_adapter(pane["command"], pane["title"], pane.get("cmdline", ""))
                ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
                status = self.status_engine.evaluate(composite_key, pane["dead"], adapter, ctx)
                visit = self.visits.visit_label(remote_session, pane["pane_id"])

                custom_project = self.overrides.get_project(composite_key)
                custom_agent = self.overrides.get_agent(composite_key)
                # Remote title overrides aren't pushed to the real remote
                # tmux (no remote install -- see docs/ARCHITECTURE.md), so
                # the override itself is the effective title here.
                effective_title = self.overrides.get_title(composite_key) or pane["title"]
                basename = Path(pane["path"] or "").name or pane["path"]

                auto_project = render.resolve_display_project(
                    None, None, effective_title, basename, name, no_name
                )
                project = custom_project or auto_project
                agent = custom_agent or adapter.name
                title_line = render.title_secondary_line(project, effective_title, name)
                # Remote captures are title-only (see docs/ARCHITECTURE.md),
                # so activity extraction has nothing to search -- almost
                # always None here, which is honest given the evidence.
                activity_text, duration_seconds = self._activity_and_duration(
                    composite_key, adapter, ctx, status, pane["dead"]
                )

                self.notifier.observe(composite_key, status, project)

                out.append(
                    {
                        "host": name,
                        "project": project,
                        "auto_project": auto_project,
                        "agent": agent,
                        "auto_agent": adapter.name,
                        "title_line": title_line,
                        "activity_text": activity_text,
                        "duration_seconds": duration_seconds,
                        "pane_title": effective_title,
                        "path": pane["path"],
                        "status": status,
                        "result_state": "none",
                        "visit": visit,
                        "key": composite_key,
                        "session": pane.get("session"),
                        "window_index": pane.get("window_index"),
                        "window_name": pane.get("window_name") or "",
                        "pane_index": pane.get("pane_index"),
                        "pane_id": pane.get("pane_id"),
                        "kind": "pane",
                        "remote": True,
                        "offline": False,
                    }
                )

        return out

    def load(self) -> None:
        previous_key = None
        if self.visible_rows and 0 <= self.selected < len(self.visible_rows):
            previous_key = self.visible_rows[self.selected]["key"]

        now = time.monotonic()
        self.rows = self._local_rows() + self._remote_rows(now)
        self._apply_filter(previous_key)

    def _apply_filter(self, previous_key: Optional[str] = None) -> None:
        """Recomputes ``visible_rows``/``visual`` from ``rows`` + the
        current search filter, trying to keep the same row selected by
        key (falling back to clamping the index) -- called both after a
        fresh ``load()`` and whenever the filter text itself changes.
        """

        if previous_key is None and self.visible_rows and 0 <= self.selected < len(self.visible_rows):
            previous_key = self.visible_rows[self.selected]["key"]

        self.visible_rows = [r for r in self.rows if render.row_matches_filter(r, self.filter_text)]

        if self.attention_mode:
            # A separate, deliberately re-sorted presentation the user
            # explicitly asked for (see toggle_attention()) -- this is NOT
            # the default list silently reordering itself on a refresh.
            self.visible_rows = render.sort_by_attention(self.visible_rows)

        if self.navigator_mode:
            self.visible_rows = navigator_rows(self.visible_rows)

        if not self.visible_rows:
            self.selected = 0
            self.visual = []
            return

        if previous_key:
            for idx, row in enumerate(self.visible_rows):
                if row["key"] == previous_key:
                    self.selected = idx
                    break
            else:
                self.selected = min(self.selected, len(self.visible_rows) - 1)
        else:
            self.selected = min(self.selected, len(self.visible_rows) - 1)

        self._build_visual()

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self._apply_filter()

    def clear_filter(self) -> None:
        self.set_filter("")

    def _build_visual(self) -> None:
        if self.navigator_mode:
            visual = []
            seen = []
            for idx, row in enumerate(self.visible_rows):
                if row.get("kind") == "window":
                    label = f'Session {row.get("session")}'
                elif row.get("remote"):
                    label = row.get("host") or ""
                else:
                    label = ""
                if label and label not in seen:
                    seen.append(label)
                    visual.append({"type": "header", "host": label})
                visual.append({"type": "data", "row": row, "row_index": idx})
            self.visual = visual
            return

        if self.attention_mode:
            # Flat, priority-sorted, deliberately NOT grouped by host --
            # the point is "what needs me right now," not "what's on
            # which machine."
            self.visual = [{"type": "header", "host": t("attention.title")}] + [
                {"type": "data", "row": row, "row_index": idx} for idx, row in enumerate(self.visible_rows)
            ]
            return

        visual = []
        hosts_seen = []
        for row in self.visible_rows:
            if row["host"] not in hosts_seen:
                hosts_seen.append(row["host"])

        for host in hosts_seen:
            visual.append({"type": "header", "host": host})
            for idx, row in enumerate(self.visible_rows):
                if row["host"] == host:
                    visual.append({"type": "data", "row": row, "row_index": idx})

        self.visual = visual

    def toggle_attention(self) -> None:
        self.attention_mode = not self.attention_mode
        if self.attention_mode:
            self.navigator_mode = False
        self._apply_filter()

    def toggle_navigator(self) -> None:
        """V: tmux session/window/pane tree. The project list stays the default."""

        self.navigator_mode = not self.navigator_mode
        if self.navigator_mode:
            self.attention_mode = False
        self._apply_filter()

    # -- actions --------------------------------------------------------

    def move_up(self) -> None:
        if not self.visible_rows:
            return
        self.selected = (self.selected - 1) % len(self.visible_rows)

    def move_down(self) -> None:
        if not self.visible_rows:
            return
        self.selected = (self.selected + 1) % len(self.visible_rows)

    def control_key(self) -> Optional[str]:
        """Enter. Opens the in-Tower control view. Does not move tmux.

        A window row resolves that window's active pane id and opens it.
        A missing window refreshes and stays on the list.
        """

        if not self.visible_rows:
            return None
        row = self.visible_rows[self.selected]
        if enter_intent(row) != "control":
            return None
        if row.get("kind") == "window":
            pane_id = active_pane_in_window(str(row.get("session") or ""), str(row.get("window_index") or ""))
            if not pane_id:
                self.notice = "stale"
                self.load()
                return None
            return pane_id
        if row.get("pane_id"):
            self.visits.mark_seen(self.session, row["pane_id"])
        return row.get("key")

    def edit_selected(self, stdscr) -> None:
        """The "E" menu: edit this pane's *display* identity only.

        Project name, agent label, and pane title here are all metadata
        Tower shows about a pane -- never a way to control it. The agent
        label in particular can be set to any free text (see
        ``state/overrides.py``'s module docstring): it relabels the row,
        it never changes, restarts, or sends anything to the real process.
        """

        if not self.visible_rows:
            return
        row = self.visible_rows[self.selected]
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

        def _context(current: Optional[str], auto: Optional[str]) -> List[str]:
            lines = []
            if current:
                lines.append(f'{t("edit.current_label")}: {current}')
            if auto:
                lines.append(f'{t("edit.auto_label")}: {auto}')
            return lines

        if pick.selected_key == "project":
            context = _context(self.overrides.get_project(key), row.get("auto_project"))
            name = prompt_text(stdscr, t("prompt.rename"), context_lines=context)
            if name:
                update_identity(self, key, {"project": name})

        elif pick.selected_key == "agent":
            agent_items = [(label, label) for label in AGENT_LAUNCH_ORDER]
            agent_items.append(("__custom__", t("menu.manual_agent_entry")))
            agent_items.append(("__auto__", t("menu.use_auto_agent")))
            agent_pick = run_list_picker(stdscr, t("menu.agent_name"), agent_items, footer_hint=t("wizard.hint_list"))

            if agent_pick.cancelled or agent_pick.selected_key is None:
                pass
            elif agent_pick.selected_key == "__custom__":
                context = _context(self.overrides.get_agent(key), row.get("auto_agent"))
                custom = prompt_text(stdscr, t("prompt.agent_name"), context_lines=context)
                if custom:
                    update_identity(self, key, {"agent": custom})
            elif agent_pick.selected_key == "__auto__":
                update_identity(self, key, {"agent": None})
            else:
                update_identity(self, key, {"agent": agent_pick.selected_key})

        elif pick.selected_key == "title":
            context = _context(self.overrides.get_title(key), row.get("pane_title"))
            title = prompt_text(stdscr, t("prompt.pane_title"), context_lines=context)
            if title:
                update_identity(self, key, {"title": title})

        elif pick.selected_key == "reset":
            update_identity(self, key, {"reset": True})

        self.load()

    def status_counts(self, host: Optional[str] = None) -> Dict[str, int]:
        """Counts reflect the search filter (if any) -- see the module
        docstring's note on ``visible_rows`` vs. ``rows``.
        """

        rows = self.visible_rows if host is None else [r for r in self.visible_rows if r["host"] == host]
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


def _hosts_in_order(tower: Tower) -> List[str]:
    hosts: List[str] = []
    for row in tower.visible_rows:
        if row["host"] not in hosts:
            hosts.append(row["host"])
    return hosts


def _status_label(status: str) -> str:
    return t("status." + status)


def _project_text(row: Dict) -> str:
    return row["project"] if row.get("project") is not None else t(row.get("placeholder", "remote.unreachable"))


def _duration_text(tower: Tower, row: Dict) -> str:
    if not tower.config.get("show_status_duration"):
        return ""
    seconds = row.get("duration_seconds")
    if not seconds:
        return ""
    return render.format_duration(seconds)


def _build_detail_fields(tower: Tower, row: Dict) -> List[Tuple[str, str]]:
    if row.get("kind") == "window":
        return [
            (t("detail.session"), row.get("session") or ""),
            (t("detail.window"), f'{row.get("window_index")}: {row.get("window_name") or ""}'),
            (t("detail.nav_target"), t("detail.nav_window_hint")),
        ]

    status = row["status"]
    status_text = f'{STATUS_SYMBOL.get(status, "?")} {t("status." + status)}'
    duration = _duration_text(tower, row)
    if duration:
        status_text = f"{status_text} · {duration}"

    fields = [
        (t("detail.project"), _project_text(row)),
        (t("detail.agent"), row.get("agent")),
        (t("detail.activity"), row.get("activity_text") if tower.config.get("show_activity") else None),
        (t("detail.pane_title"), row.get("pane_title")),
        (t("detail.path"), row.get("path")),
        (t("detail.status"), status_text),
        (t("detail.host"), row.get("host")),
    ]
    fields.extend(_location_fields(row))
    if row.get("pane_id"):
        fields.append((t("detail.pane_id"), row["pane_id"]))
    return fields


def _location_fields(row: Dict) -> List[Tuple[str, str]]:
    """Where this pane sits in tmux, from the row's own metadata."""

    fields: List[Tuple[str, str]] = []
    if row.get("session"):
        fields.append((t("detail.session"), str(row["session"])))
    if row.get("window_index") is not None and row.get("window_name") is not None and "window_index" in row:
        fields.append((t("detail.window"), f'{row.get("window_index")}: {row.get("window_name")}'))
    if "pane_index" in row and row.get("pane_index") is not None:
        fields.append((t("detail.pane_index"), str(row["pane_index"])))
    if "pane_active" in row:
        fields.append(
            (t("detail.active"), t("detail.active_yes") if row.get("pane_active") else t("detail.active_no"))
        )
    return fields


def _build_physical_lines(tower: Tower, narrow: bool) -> List[Dict]:
    """Flattens ``tower.visual`` (header/data items) into one entry per
    *physical* terminal line, since a data row can now take up to 4 lines
    (project / agent+status in narrow layout / pane-title secondary line /
    current-activity line) -- viewport scrolling paginates over this
    list, not over ``visual``.
    """

    physical: List[Dict] = []

    for i, item in enumerate(tower.visual):
        if item["type"] == "header":
            physical.append({"kind": "header", "item_index": i, "host": item["host"]})
            continue

        row = item["row"]
        row_index = item["row_index"]
        physical.append({"kind": "primary", "row": row, "row_index": row_index})
        if row.get("kind") == "window":
            continue

        if narrow:
            physical.append({"kind": "agent_status", "row": row, "row_index": row_index})

        if row.get("title_line"):
            physical.append({"kind": "secondary", "row": row, "row_index": row_index})

        if row.get("activity_text"):
            physical.append({"kind": "activity", "row": row, "row_index": row_index})

    return physical


def draw(stdscr, tower: Tower, filtering: bool = False, remote_state: str = "stopped") -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    narrow = render.use_narrow_layout(width)

    # -- row 0: title + per-host mini summaries (right-aligned) ---------

    title = t("app.title")
    safe_add(stdscr, 0, 2, title, curses.A_BOLD)
    from .remote_menu import badge_text

    badge = badge_text(remote_state)
    badge_x = 2 + render.display_width(title) + 3
    safe_add(stdscr, 0, badge_x, badge, curses.A_DIM)

    host_bits = [
        f"{host} {summary}"
        for host in _hosts_in_order(tower)
        for summary in [render.format_host_summary(tower.status_counts(host), _status_label)]
        if summary
    ]
    host_line = "   ".join(host_bits)
    if host_line and not narrow:
        min_x = badge_x + render.display_width(badge) + 2
        x = max(min_x, width - render.display_width(host_line) - 2)
        if x + render.display_width(host_line) < width:
            safe_add(stdscr, 0, x, host_line, curses.A_BOLD)

    # -- row 1: hint line, or the live search-filter input ---------------

    if filtering:
        safe_add(stdscr, 1, 2, f'{t("filter.label")} {tower.filter_text}_', curses.A_BOLD)
    else:
        hint_keys = [
            t("hint.move"),
            t("hint.open"),
            t("hint.rename"),
            t("hint.add_project"),
            t("hint.new_workspace"),
            t("hint.remote"),
            t("hint.settings"),
        ]
        hint_keys.append(t("hint.filter_clear") if tower.filter_text else t("hint.filter"))
        hint_keys.append(t("hint.navigator"))
        hint_keys.append(t("hint.attention"))
        hint_keys += [t("hint.refresh"), t("hint.quit")]
        safe_add(stdscr, 1, 2, "   ".join(hint_keys), curses.A_DIM)

    start_y = 3
    bottom_hint_y = height - 1

    # -- selected-item detail panel (bottom), skipped on a short terminal -

    detail_lines: List[str] = []
    if render.should_show_detail_panel(height) and tower.visible_rows:
        selected_row = tower.visible_rows[tower.selected]
        detail_lines = render.format_detail_panel(_build_detail_fields(tower, selected_row))

    detail_block = (1 + 1 + len(detail_lines)) if detail_lines else 0  # divider + title + fields
    max_rows = max(0, bottom_hint_y - start_y - detail_block)

    if not tower.visible_rows:
        msg = t("wizard.no_matches") if tower.filter_text else t("empty.no_panes")
        safe_add(stdscr, start_y, 2, msg)
        safe_add(stdscr, bottom_hint_y, 2, t("footer.return_hint"), curses.A_DIM)
        stdscr.noutrefresh()
        curses.doupdate()
        return

    physical = _build_physical_lines(tower, narrow)

    selected_phys_index = 0
    for i, p in enumerate(physical):
        if p["kind"] == "primary" and p["row_index"] == tower.selected:
            selected_phys_index = i
            break

    top = 0
    if selected_phys_index >= max_rows:
        top = selected_phys_index - max_rows + 1

    visible_physical = physical[top : top + max_rows]

    agent_x = max(34, width - 34)
    status_x = max(50, width - 16)

    for offset, p in enumerate(visible_physical):
        y = start_y + offset

        if p["kind"] == "header":
            label = f'── {p["host"]} '
            safe_add(stdscr, y, 0, label + "─" * max(0, width - len(label) - 1), curses.A_BOLD)
            continue

        row = p["row"]
        is_selected = p["row_index"] == tower.selected

        base_attr = curses.A_REVERSE
        if is_selected and curses.has_colors():
            base_attr = curses.color_pair(6) | curses.A_BOLD

        if is_selected:
            safe_add(stdscr, y, 0, " " * max(1, width - 1), base_attr)

        if p["kind"] == "primary" and row.get("kind") == "window":
            marker = ">" if is_selected else " "
            safe_add(stdscr, y, 0, f"{marker} {row.get('project') or ''}", base_attr if is_selected else curses.A_BOLD)
            continue

        if p["kind"] == "primary":
            marker = ">" if is_selected else " "
            new_flag = t("marker.new") if row.get("visit") == "NEW" else ""
            symbol = STATUS_SYMBOL.get(row["status"], "?")
            prefix = f"{marker} {new_flag:<3} {symbol} "

            project_width = (width - len(prefix) - 2) if narrow else max(10, agent_x - len(prefix) - 2)
            label = _project_text(row)
            if row.get("attention") == "approval_required":
                label = "! " + label
            elif row.get("attention") == "input_required":
                label = "? " + label
            elif row.get("attention") == "error":
                label = "! " + label
            text = render.truncate_to_width(label, project_width)

            safe_add(stdscr, y, 0, prefix, base_attr if is_selected else curses.A_BOLD)
            safe_add(stdscr, y, len(prefix), text, base_attr if is_selected else 0)

            if not narrow:
                safe_add(stdscr, y, agent_x, row["agent"][:14], base_attr)
                status_attr_here = base_attr if is_selected else status_attr(row["status"])
                status_text = t("status." + row["status"])
                duration = _duration_text(tower, row)
                if duration:
                    status_text = f"{status_text} · {duration}"
                safe_add(stdscr, y, status_x, status_text, status_attr_here)

        elif p["kind"] == "agent_status":
            text = render.agent_status_line(row["agent"], t("status." + row["status"]), _duration_text(tower, row))
            attr = base_attr if is_selected else curses.A_DIM
            safe_add(stdscr, y, 4, text, attr)

        elif p["kind"] == "secondary":
            attr = base_attr if is_selected else curses.A_DIM
            safe_add(stdscr, y, 4, row["title_line"], attr)

        elif p["kind"] == "activity":
            attr = base_attr if is_selected else curses.A_DIM
            safe_add(stdscr, y, 4, row["activity_text"], attr)

    # -- detail panel ------------------------------------------------------

    if detail_lines:
        divider_y = start_y + max_rows
        safe_add(stdscr, divider_y, 0, "─" * max(0, width - 1), curses.A_DIM)
        safe_add(stdscr, divider_y + 1, 2, t("detail.title"), curses.A_BOLD)
        for i, line in enumerate(detail_lines):
            safe_add(stdscr, divider_y + 2 + i, 2, line)

    footer = t("nav.stale") if tower.notice else f'{t("footer.return_hint")}   {t("footer.best_effort")}'
    safe_add(stdscr, bottom_hint_y, 2, footer, curses.A_DIM)

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
        # Remote keeps running after Q. Stopping it is M → 원격 종료.
        from ..server import service

        service.on_tui_exit()
        if own_pane_id:
            registration.unregister_if_self(session, own_pane_id)


def _remote_state() -> str:
    from ..server import service

    try:
        return service.status().state
    except Exception:
        return "error"


def _run_loop(stdscr, session: str, own_pane_id: str) -> None:
    from ..server import service

    tower = Tower(session, own_pane_id, results=ResultTracker())
    tower.load()
    tower.last_refresh = time.monotonic()
    filtering = False
    try:
        # Autostart binds the remote to this Tower's session -- never a guess.
        service.maybe_autostart(session=tower.session, own_pane_id=tower.own_pane_id)
    except Exception:
        pass
    remote_state = _remote_state()
    draw(stdscr, tower, remote_state=remote_state)

    while True:
        now = time.monotonic()

        if not filtering and now - tower.last_refresh >= REFRESH_SECONDS:
            tower.load()
            tower.last_refresh = now
            remote_state = _remote_state()
            draw(stdscr, tower, filtering=filtering, remote_state=remote_state)

        try:
            key = read_key(stdscr)
        except KeyboardInterrupt:
            break

        if key == -1:
            continue

        if key == curses.KEY_RESIZE:
            draw(stdscr, tower, filtering=filtering, remote_state=remote_state)
            continue

        if is_ctrl_c(key):
            break

        # -- live search filter ("/" to start typing; Esc always clears) --

        if filtering:
            if is_escape(key):
                tower.clear_filter()
                filtering = False
            elif is_enter(key):
                filtering = False
            elif is_backspace(key):
                tower.set_filter(tower.filter_text[:-1])
            elif isinstance(key, str) and key.isprintable():
                tower.set_filter(tower.filter_text + key)
            draw(stdscr, tower, filtering=filtering, remote_state=remote_state)
            continue

        if key == "/":
            filtering = True
            draw(stdscr, tower, filtering=filtering, remote_state=remote_state)
            continue

        if is_escape(key) and tower.filter_text:
            tower.clear_filter()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_UP or matches_letter(key, "k"):
            tower.move_up()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_DOWN or matches_letter(key, "j"):
            tower.move_down()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if is_enter(key):
            pane_key = tower.control_key()
            if pane_key:
                from .control_view import open_control_view

                open_control_view(stdscr, tower, pane_key)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "e"):
            tower.edit_selected(stdscr)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "r"):
            tower.load()
            tower.last_refresh = time.monotonic()
            remote_state = _remote_state()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "a"):
            tower.toggle_attention()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "v"):
            tower.toggle_navigator()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "m"):
            from .remote_menu import open_remote_menu

            open_remote_menu(stdscr, tower)
            remote_state = _remote_state()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "c"):
            from .settings_menu import open_settings

            open_settings(stdscr)
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "n"):
            run_launcher(stdscr, tower, multi=False, state_dir=STATE_DIR)
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if matches_letter(key, "w"):
            run_launcher(stdscr, tower, multi=True, state_dir=STATE_DIR)
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower, remote_state=remote_state)
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
