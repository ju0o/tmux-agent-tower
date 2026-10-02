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
from ..detection.identity import identify_agent, prefer_override
from ..detection.topology import agent_through_ssh, classify_transport, peer_topology
from ..hostreg import HostRegistry, openssh_hostname
from ..adapters.base import PaneContext
from ..detection.result import ResultTracker
from ..detection.status import StatusEngine, STATUS_DEAD
from .. import i18n
from ..i18n import t
from ..launcher.config import AGENT_LAUNCH_ORDER, load_config
from .. import notify
from ..remote.collector import fetch_remote, HOST_STATUS_ONLINE
from ..state.bindings import ProjectBindingStore
from ..state.overrides import OverrideStore
from ..detection.project import git_project_name
from ..state.visits import VisitStore
from ..tmux import capture as tmux_capture
from ..tmux import discovery
from ..tmux import registration
from ..control.actions import enter_intent, update_identity
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


def location_lines(row: Dict) -> List[str]:
    """Execution host, the tmux object that receives input, and the link."""

    execution = row.get("execution_host") or row.get("host") or "-"
    tmux_host = row.get("tmux_host") or execution
    session = row.get("session") or "-"
    window = row.get("window_index")
    window_text = "-" if window is None else str(window)
    pane = row.get("pane_id") or row.get("key") or "-"
    lines = [
        f'{t("detail.execution_host")}  {execution}',
        f'{t("detail.tmux_place")}  {tmux_host} → Session {session} → Window {window_text} → Pane {pane}',
    ]
    if row.get("transport") == "ssh":
        target = row.get("transport_target") or execution
        lines.append(f'{t("detail.transport")}  SSH → {target}')
    elif row.get("remote"):
        lines.append(t("detail.peer_readonly"))
    else:
        lines.append(f'{t("detail.transport")}  {t("detail.transport_local")}')
    return lines


def _place_label(topology: Dict, pane: Dict) -> str:
    """Where an SSH client pane sits, without pretending it is that host's tmux."""

    if topology.get("transport") != "ssh":
        return ""
    index = pane.get("window_index")
    return t("nav.via_tmux").format(
        tmux_host=topology.get("tmux_host") or "",
        index="" if index is None else index,
        pane=pane.get("pane_id") or "",
    )


def _window_group(row: Dict) -> Tuple:
    """Identity of the window a pane belongs to. Names are not part of it.

    An SSH client pane is not a member of that window's local group.
    It is shown under the execution host, still controlled by this
    tmux pane id. A peer tmux snapshot uses its own server identity.
    """

    host = str(row.get("host") or "")
    session = str(row.get("session") or "")
    window_id = str(row.get("window_id") or "")
    index = str(row.get("window_index") if row.get("window_index") is not None else "")
    pane_id = str(row.get("pane_id") or "")
    if row.get("transport") == "ssh" and not row.get("remote"):
        return ("ssh", str(row.get("tmux_host") or ""), session, pane_id)
    if row.get("remote"):
        return ("remote", str(row.get("tmux_host") or host), session, window_id or index or "?")
    if window_id:
        return ("local", window_id)
    return ("local", session, index)


def _assign_tree_guides(rows: List[Dict]) -> None:
    """Box-drawing prefixes. Width is measured with ``display_width``
    at draw time, so a Korean name does not shift the badge.
    """

    grouped: List[List[Dict]] = []
    for row in rows:
        host = str(row.get("host") or "")
        if not grouped or str(grouped[-1][0].get("host") or "") != host:
            grouped.append([])
        grouped[-1].append(row)
    for group in grouped:
        windows = [row for row in group if row.get("kind") == "window"]
        for index, window in enumerate(windows):
            last = index == len(windows) - 1
            if window.get("collapsed"):
                window["guide"] = "└▸ " if last else "├▸ "
            else:
                window["guide"] = "└─ " if last else "├─ "
            panes = [row for row in group if row.get("kind") == "pane" and row.get("tree_window") == window.get("key")]
            stem = "   " if last else "│  "
            for pane_index, pane in enumerate(panes):
                end = pane_index == len(panes) - 1
                pane["guide"] = stem + ("└─ " if end else "├─ ")


def navigator_rows(panes: List[Dict], collapsed: Optional[set] = None) -> List[Dict]:
    """Host, then a selectable window row, then that window's panes.

    The window row's key is ``window_id`` locally, and host plus id for
    a remote window, so two hosts can both have ``@1``. A renamed window
    stays the same row. Enter on it opens Window Control and does not
    move the client. ``collapsed`` only hides child rows.
    """

    closed = collapsed or set()
    order: List[Tuple] = []
    buckets: Dict[Tuple, List[Dict]] = {}
    for row in panes:
        if row.get("kind") == "zero" or not row.get("pane_id"):
            continue
        key = _window_group(row)
        buckets.setdefault(key, []).append(row)
        if key not in order:
            order.append(key)

    out: List[Dict] = []
    for key in order:
        members = buckets[key]
        sample = members[0]
        session = str(sample.get("session") or "")
        index = str(sample.get("window_index") if sample.get("window_index") is not None else "")
        name = str(sample.get("window_name") or "")
        window_id = str(sample.get("window_id") or "")
        remote = bool(sample.get("remote"))
        via_ssh = sample.get("transport") == "ssh" and not remote
        if remote:
            row_key = f'{sample.get("tmux_host") or sample.get("host") or ""}:{window_id or index}'
        elif via_ssh:
            row_key = f'via:{sample.get("tmux_host") or ""}:{sample.get("pane_id") or ""}'
        else:
            row_key = window_id or f"win:{session}:{index}"
        if via_ssh:
            label = t("nav.via_tmux").format(
                tmux_host=sample.get("tmux_host") or "",
                index=index,
                pane=sample.get("pane_id") or "",
            )
        elif remote:
            label = t("nav.peer_tmux").format(index=index, name=name)
        else:
            label = t("nav.window_row").format(index=index, name=name)
        group = f"win:{sample.get('host') or ''}:{session}:{window_id or index}"
        summary = render.window_summary(members)
        hidden = row_key in closed
        out.append(
            {
                "kind": "window",
                "key": row_key,
                "window_id": window_id,
                "project": label,
                "agent": "",
                "summary": summary,
                "status": "IDLE",
                "host": sample.get("host") or "",
                "session": session,
                "window_index": index,
                "window_name": name,
                "pane_count": len(members),
                "nav_group": group,
                "nav_group_label": label,
                "remote": remote,
                "collapsed": hidden,
            }
        )
        if hidden:
            continue
        for row in members:
            item = dict(row)
            item["kind"] = "pane"
            item["nav_group"] = group
            item["nav_group_label"] = label
            item["tree_window"] = row_key
            out.append(item)
    _assign_tree_guides(out)
    return out


def zero_state_rows() -> List[Dict]:
    """What to show when Tower has no working pane yet."""

    title = t("zero.title")
    actions = (
        ("task", t("zero.task")),
        ("window", t("zero.window")),
        ("pane", t("zero.pane")),
        ("workspace", t("zero.workspace")),
    )
    return [
        {
            "kind": "zero",
            "key": f"zero:{action}",
            "action": action,
            "project": label,
            "agent": "",
            "status": "IDLE",
            "host": title,
            "remote": False,
        }
        for action, label in actions
    ]


class Tower:
    def __init__(self, session: str, own_pane_id: str = "", status_engine: Optional[StatusEngine] = None, results: Optional[ResultTracker] = None):
        self.session = session
        self.own_pane_id = own_pane_id
        self.local_host = local_host_label()
        self.remote_hosts = load_remote_hosts()
        self._host_registry = HostRegistry(self.local_host, _raw_hostname(), self.remote_hosts)

        self.visits = VisitStore(STATE_DIR)
        self.overrides = OverrideStore(STATE_DIR / "overrides.json")
        self.bindings = ProjectBindingStore(STATE_DIR / "project-bindings.json")
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
        self.navigator_mode = True           # the tree is the default; V returns here
        self.collapsed: set = set()          # window row keys hidden in the UI only
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

    def _binding_name(self, key: str, session: Optional[str], pane_pid: Optional[str]) -> Optional[str]:
        """Launcher binding for this pane, or None when it does not match.

        The git name at the bound path wins over the name stored at
        creation, but only while that directory is still there.
        """

        record = self.bindings.usable(key, session or "", pane_pid or "")
        if not record:
            return None
        if str(record.get("transport") or "") == "ssh":
            # The path is on the far side. A local directory with the same
            # text is not that project.
            return (record.get("project_name") or "").strip() or None
        git_name = git_project_name(record.get("project_path") or "")
        return git_name or (record.get("project_name") or "").strip() or None

    def _identity(
        self,
        key: str,
        command: str,
        title: str,
        cmdline: str,
        lines,
        git_project: Optional[str],
        basename: Optional[str],
        host: str,
        no_name: str,
        binding_name: Optional[str] = None,
        process_git: Optional[str] = None,
        session: str = "",
        pane_pid: str = "",
    ) -> Dict:
        """Displayed names plus where they came from.

        The override, when present, replaces the label only. ``auto_*``
        stays the fresh detection so the next refresh can show a shell
        again after Claude exits, and so Reset Auto has something to
        return to.
        """

        auto_agent, auto_agent_source = identify_agent(command, title, cmdline, lines)
        auto_project, auto_project_source = render.resolve_project_identity(
            None,
            git_project,
            title,
            basename,
            host,
            no_name,
            binding_name=binding_name,
            process_git_name=process_git,
        )
        project, project_source = prefer_override(
            auto_project, auto_project_source, self.overrides.get_project(key, session, pane_pid)
        )
        agent, agent_source = prefer_override(
            auto_agent, auto_agent_source, self.overrides.get_agent(key, session, pane_pid)
        )
        return {
            "project": project,
            "auto_project": auto_project,
            "project_source": project_source,
            "auto_project_source": auto_project_source,
            "agent": agent,
            "auto_agent": auto_agent,
            "agent_source": agent_source,
            "auto_agent_source": auto_agent_source,
        }

    def _resolve_ssh(self, token: str) -> str:
        """OpenSSH's local reading of an alias. Cached on the registry."""

        return openssh_hostname(token)

    def _local_rows(self) -> List[Dict]:
        panes = discovery.list_panes(self.session, self.own_pane_id, CAPTURE_LINES)
        out = []
        no_name = t("project.no_name")

        for pane in panes:
            adapter = resolve_adapter(
                pane["command"], pane["title"], pane.get("cmdline") or "", pane.get("lines") or ()
            )
            ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
            status = self.status_engine.evaluate(pane["pane_id"], pane["dead"], adapter, ctx)
            visit = self.visits.visit_label(self.session, pane["pane_id"])

            key = pane["pane_id"]
            session_name = pane.get("session") or ""
            pane_pid = pane.get("pane_pid") or ""
            bound = self.bindings.usable(key, session_name, pane_pid) or {}
            topology = classify_transport(
                tmux_host=self.local_host,
                registry=self._host_registry,
                ssh_target=pane.get("ssh_target") or "",
                ssh_stale=bool(pane.get("ssh_stale")),
                resolver=self._resolve_ssh,
                binding=bound,
                override_host=self.overrides.get_execution_host(key, session_name, pane_pid) or "",
            )
            via_ssh = topology["transport"] == "ssh"
            effective_title = pane["title"]  # local title edits are pushed to real tmux -- see edit_selected
            identity = self._identity(
                key,
                pane["command"],
                "" if via_ssh else effective_title,
                pane.get("cmdline") or "",
                pane.get("lines") or (),
                None if via_ssh else pane.get("git_project"),
                None if via_ssh else pane.get("path_basename"),
                _raw_hostname(),
                no_name,
                binding_name=self._binding_name(key, session_name, pane_pid),
                process_git=None if via_ssh else pane.get("process_git"),
                session=session_name,
                pane_pid=pane_pid,
            )
            if via_ssh:
                agent_name, agent_source = agent_through_ssh(
                    identity["agent"],
                    identity["agent_source"],
                    effective_title,
                    pane.get("lines") or (),
                )
                identity["agent"] = agent_name
                identity["agent_source"] = agent_source
            project = identity["project"]
            agent = identity["agent"]
            auto_project = identity["auto_project"]
            title_line = render.title_secondary_line(project, effective_title, _raw_hostname())
            activity_text, duration_seconds = self._activity_and_duration(key, adapter, ctx, status, pane["dead"])
            result_state = self._result_state(key, status, adapter, ctx, pane["dead"])
            attention = "none" if pane["dead"] else adapter.detect_attention(ctx)
            attention_prompt = adapter.extract_attention_prompt(ctx) if attention != "none" else ""
            notify_status = "WAITING" if attention in ("approval_required", "input_required") else status

            self.notifier.observe(key, notify_status, project)

            out.append(
                {
                    "host": topology["execution_host"],
                    "observer_host": topology["observer_host"],
                    "tmux_host": topology["tmux_host"],
                    "execution_host": topology["execution_host"],
                    "transport": topology["transport"],
                    "transport_target": topology["transport_target"],
                    "topology_source": topology["topology_source"],
                    "place_label": _place_label(topology, pane),
                    "project": project,
                    "auto_project": auto_project,
                    "project_source": identity["project_source"],
                    "auto_project_source": identity["auto_project_source"],
                    "agent": agent,
                    "auto_agent": identity["auto_agent"],
                    "agent_source": identity["agent_source"],
                    "auto_agent_source": identity["auto_agent_source"],
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
                    "session": session_name,
                    "window_id": pane.get("window_id") or "",
                    "window_index": pane["window_index"],
                    "window_name": pane.get("window_name") or "",
                    "pane_index": pane.get("pane_index"),
                    "pane_active": bool(pane.get("pane_active")),
                    "pane_id": pane["pane_id"],
                    "pane_pid": pane.get("pane_pid") or "",
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
                adapter = resolve_adapter(
                    pane["command"], pane["title"], pane.get("cmdline") or "", pane.get("lines") or ()
                )
                ctx = PaneContext(title=pane["title"], command=pane["command"], lines=tuple(pane["lines"]))
                status = self.status_engine.evaluate(composite_key, pane["dead"], adapter, ctx)
                visit = self.visits.visit_label(remote_session, pane["pane_id"])

                # Remote title overrides aren't pushed to the real remote
                # tmux (no remote install -- see docs/ARCHITECTURE.md), so
                # the override itself is the effective title here.
                effective_title = (
                    self.overrides.get_title(composite_key, pane.get("session") or "", pane.get("pane_pid") or "")
                    or pane["title"]
                )
                basename = Path(pane["path"] or "").name or pane["path"]
                identity = self._identity(
                    composite_key,
                    pane["command"],
                    effective_title,
                    pane.get("cmdline") or "",
                    pane.get("lines") or (),
                    None,
                    basename,
                    name,
                    no_name,
                    session=pane.get("session") or "",
                    pane_pid=pane.get("pane_pid") or "",
                )
                project = identity["project"]
                agent = identity["agent"]
                auto_project = identity["auto_project"]
                title_line = render.title_secondary_line(project, effective_title, name)
                # Remote captures are title-only (see docs/ARCHITECTURE.md),
                # so activity extraction has nothing to search -- almost
                # always None here, which is honest given the evidence.
                activity_text, duration_seconds = self._activity_and_duration(
                    composite_key, adapter, ctx, status, pane["dead"]
                )

                self.notifier.observe(composite_key, status, project)
                topo = peer_topology(self.local_host, name)

                out.append(
                    {
                        "host": topo["execution_host"],
                        "observer_host": topo["observer_host"],
                        "tmux_host": topo["tmux_host"],
                        "execution_host": topo["execution_host"],
                        "transport": topo["transport"],
                        "transport_target": topo["transport_target"],
                        "topology_source": topo["topology_source"],
                        "place_label": t("nav.peer_tmux").format(
                            index=pane.get("window_index") if pane.get("window_index") is not None else "",
                            name=pane.get("window_name") or "",
                        ),
                        "project": project,
                        "auto_project": auto_project,
                        "project_source": identity["project_source"],
                        "auto_project_source": identity["auto_project_source"],
                        "agent": agent,
                        "auto_agent": identity["auto_agent"],
                        "agent_source": identity["agent_source"],
                        "auto_agent_source": identity["auto_agent_source"],
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
                        "window_id": pane.get("window_id") or "",
                        "window_index": pane.get("window_index"),
                        "window_name": pane.get("window_name") or "",
                        "pane_index": pane.get("pane_index"),
                        "pane_id": pane.get("pane_id"),
                        "pane_pid": pane.get("pane_pid") or "",
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

        local_work = [r for r in self.visible_rows if r.get("pane_id") and not r.get("remote")]
        if not self.filter_text and not local_work:
            remotes = [r for r in self.visible_rows if r.get("remote")]
            self.visible_rows = zero_state_rows() + navigator_rows(remotes)
        elif self.attention_mode:
            # A flat priority list. The tree itself is not reordered.
            panes = [r for r in self.visible_rows if r.get("pane_id")]
            self.visible_rows = render.sort_by_attention(panes)
        else:
            closed = set() if self.filter_text else self.collapsed
            self.visible_rows = navigator_rows(self.visible_rows, closed)

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
        if not self.attention_mode and any(row.get("kind") == "window" for row in self.visible_rows):
            # Host is the divider. Window and pane are both rows.
            visual = []
            seen = []
            for idx, row in enumerate(self.visible_rows):
                label = row.get("host") or ""
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
        self._apply_filter()

    def toggle_navigator(self) -> None:
        """V returns to the tree. It is not a second list."""

        self.attention_mode = False
        self.navigator_mode = True
        self._apply_filter()

    def collapse_selected(self) -> None:
        """Hide a window's panes. tmux is not changed."""

        row = self.visible_rows[self.selected] if self.visible_rows else None
        if not row:
            return
        key = row.get("key") if row.get("kind") == "window" else row.get("tree_window")
        if not key:
            return
        self.collapsed.add(key)
        self._apply_filter(key)

    def expand_selected(self) -> None:
        """Show a window's panes again. tmux is not changed."""

        row = self.visible_rows[self.selected] if self.visible_rows else None
        if not row:
            return
        key = row.get("key") if row.get("kind") == "window" else row.get("tree_window")
        if not key:
            return
        self.collapsed.discard(key)
        self._apply_filter(key)

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
        """Enter. Opens control for the selected row. Does not move tmux.

        A pane returns its pane key. A window returns its window id.
        A zero-state row returns its action key.
        """

        if not self.visible_rows:
            return None
        row = self.visible_rows[self.selected]
        intent = enter_intent(row)
        if intent == "control":
            if row.get("pane_id"):
                self.visits.mark_seen(self.session, row["pane_id"])
            return row.get("key")
        if intent == "window":
            return row.get("window_id")
        if intent == "zero":
            return row.get("key")
        return None

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
            ("execution", t("menu.execution_host")),
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
        session = str(row.get("session") or "")
        pane_pid = str(row.get("pane_pid") or "")

        def _context(current: Optional[str], auto: Optional[str]) -> List[str]:
            lines = []
            if current:
                lines.append(f'{t("edit.current_label")}: {current}')
            if auto:
                lines.append(f'{t("edit.auto_label")}: {auto}')
            return lines

        if pick.selected_key == "execution":
            context = _context(self.overrides.get_execution_host(key, session, pane_pid), row.get("execution_host"))
            name = prompt_text(stdscr, t("prompt.execution_host"), context_lines=context)
            if name:
                update_identity(self, key, {"execution_host": name})

        elif pick.selected_key == "project":
            context = _context(self.overrides.get_project(key, session, pane_pid), row.get("auto_project"))
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
                context = _context(self.overrides.get_agent(key, session, pane_pid), row.get("auto_agent"))
                custom = prompt_text(stdscr, t("prompt.agent_name"), context_lines=context)
                if custom:
                    update_identity(self, key, {"agent": custom})
            elif agent_pick.selected_key == "__auto__":
                update_identity(self, key, {"agent": None})
            else:
                update_identity(self, key, {"agent": agent_pick.selected_key})

        elif pick.selected_key == "title":
            context = _context(self.overrides.get_title(key, session, pane_pid), row.get("pane_title"))
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


def _badge_text(symbol: str, key: str) -> str:
    return f"{symbol} {t(key)}"


def _build_detail_fields(tower: Tower, row: Dict) -> List[Tuple[str, str]]:
    symbol, key = render.execution_badge(row.get("status") or "")
    status_text = _badge_text(symbol, key)
    duration = _duration_text(tower, row)
    if duration:
        status_text = f"{status_text} · {duration}"

    kind = row.get("attention") or "none"
    if kind == "approval_required":
        attention_text = _badge_text("!", "state.approval")
    elif kind == "input_required":
        attention_text = _badge_text("?", "state.input")
    elif kind == "error":
        attention_text = _badge_text("!", "state.error")
    else:
        attention_text = "-"
    if row.get("result_state") == "ready":
        result_text = _badge_text("✓", "state.result")
    elif row.get("result_state") == "read":
        result_text = t("control.result_read")
    else:
        result_text = "-"

    fields = [
        (t("detail.project"), _project_text(row)),
        (t("detail.agent"), row.get("agent")),
        (t("detail.status"), status_text),
        (t("detail.attention"), attention_text),
        (t("detail.result"), result_text),
        (t("detail.activity"), row.get("activity_text") if tower.config.get("show_activity") else None),
        (t("detail.pane_title"), row.get("pane_title")),
        (t("detail.path"), None if row.get("transport") == "ssh" else row.get("path")),
        (t("detail.execution_host"), row.get("execution_host") or row.get("host")),
        (t("detail.tmux_place"), " → ".join(
            part for part in (
                str(row.get("tmux_host") or ""),
                f'Session {row.get("session") or "-"}',
                f'Window {row.get("window_index")}',
                f'Pane {row.get("pane_id") or "-"}',
            ) if part
        )),
        (t("detail.transport"), (
            f'SSH → {row.get("transport_target") or row.get("execution_host") or ""}'
            if row.get("transport") == "ssh"
            else (t("detail.peer_readonly") if row.get("remote") else t("detail.transport_local"))
        )),
    ]
    fields.extend(_location_fields(row))
    if row.get("window_id"):
        fields.append((t("detail.window_id"), row["window_id"]))
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
            physical.append({"kind": "header", "item_index": i, "host": item["host"], "level": item.get("level", "host")})
            continue

        row = item["row"]
        row_index = item["row_index"]
        physical.append({"kind": "primary", "row": row, "row_index": row_index})

        if not narrow and row.get("activity_text"):
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

    host_line = render.format_watch_header(render.watch_counts(tower.visible_rows), t)
    if host_line and not narrow:
        min_x = badge_x + render.display_width(badge) + 2
        x = max(min_x, width - render.display_width(host_line) - 2)
        if x + render.display_width(host_line) < width:
            safe_add(stdscr, 0, x, host_line, curses.A_BOLD)

    # -- row 1: hint line, or the live search-filter input ---------------

    if filtering:
        safe_add(stdscr, 1, 2, f'{t("filter.label")} {tower.filter_text}_', curses.A_BOLD)
        start_y = 3
    else:
        hint_keys = [
            t("hint.move"),
            t("hint.fold"),
            t("hint.open"),
            t("hint.actions"),
            t("hint.attention"),
            t("hint.create"),
            t("hint.remote"),
            t("hint.filter_clear") if tower.filter_text else t("hint.filter"),
            t("hint.quit"),
        ]
        if narrow:
            hint_keys = [
                t("hint.move"),
                t("hint.open"),
                t("hint.actions"),
                t("hint.attention"),
                t("hint.quit"),
            ]
        hint = "   ".join(hint_keys)
        # One clipped line hides V/A/Q. Wrap onto a second line instead.
        if render.display_width(hint) <= max(0, width - 4):
            safe_add(stdscr, 1, 2, hint, curses.A_DIM)
            start_y = 3
        else:
            mid = (len(hint_keys) + 1) // 2
            safe_add(stdscr, 1, 2, "   ".join(hint_keys[:mid]), curses.A_DIM)
            safe_add(stdscr, 2, 2, "   ".join(hint_keys[mid:]), curses.A_DIM)
            start_y = 4
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
            if p.get("level") == "window":
                # A window is a divider above its panes, not a row.
                label = f'  ┄ {p["host"]} '
                safe_add(stdscr, y, 0, label + "┄" * max(0, width - render.display_width(label) - 1), curses.A_DIM)
                continue
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

        if p["kind"] == "primary":
            parts = render.list_row_parts(row, width, t, _duration_text(tower, row))
            prefix = parts["guide"] or (" " if not is_selected else "")
            project_text = parts["project"] if row.get("kind") != "zero" else _project_text(row)
            if row.get("kind") == "zero":
                project_text = _project_text(row)
            state_text = parts["badge"]
            agent_text = parts["agent"]
            state_width = render.display_width(state_text)
            prefix_width = render.display_width(prefix)
            project_budget = max(8, width - prefix_width - state_width - 4)
            if not narrow and agent_text:
                project_budget = max(10, agent_x - prefix_width - 2)
            text = render.truncate_to_width(project_text, project_budget)

            safe_add(stdscr, y, 0, prefix, base_attr if is_selected else curses.A_BOLD)
            safe_add(stdscr, y, prefix_width, text, base_attr if is_selected else 0)

            if agent_text:
                safe_add(stdscr, y, max(prefix_width + render.display_width(text) + 2, agent_x), agent_text[:14], base_attr)
            state_x = max(prefix_width + render.display_width(text) + 2, width - state_width - 2)
            if state_text and state_x > prefix_width + 1:
                if row.get("kind") == "window":
                    status_attr_here = base_attr if is_selected else curses.A_DIM
                else:
                    status_attr_here = base_attr if is_selected else status_attr(row.get("status") or "")
                safe_add(stdscr, y, state_x, state_text, status_attr_here)

        elif p["kind"] == "activity":
            attr = base_attr if is_selected else curses.A_DIM
            indent = render.display_width(row.get("guide") or "") or 4
            safe_add(stdscr, y, indent, row["activity_text"], attr)

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

        if key == curses.KEY_LEFT:
            tower.collapse_selected()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == curses.KEY_RIGHT:
            tower.expand_selected()
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
            row = tower.visible_rows[tower.selected] if tower.visible_rows else None
            intent = enter_intent(row)
            if intent == "control":
                pane_key = tower.control_key()
                if pane_key:
                    from .control_view import open_control_view

                    open_control_view(stdscr, tower, pane_key)
            elif intent == "window" and row:
                from .structure_menu import open_window_control

                open_window_control(stdscr, tower, row.get("window_id") or "")
            elif intent == "zero" and row:
                from .structure_menu import run_zero_action

                run_zero_action(stdscr, tower, row.get("action") or "")
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == " ":
            from .structure_menu import open_context_menu

            open_context_menu(stdscr, tower)
            tower.load()
            tower.last_refresh = time.monotonic()
            draw(stdscr, tower, remote_state=remote_state)
            continue

        if key == "+":
            from .structure_menu import open_create_hub

            open_create_hub(stdscr, tower)
            tower.load()
            tower.last_refresh = time.monotonic()
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
    # The default Esc delay is about a second, so Back felt ignored.
    import locale
    import os

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")

    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
