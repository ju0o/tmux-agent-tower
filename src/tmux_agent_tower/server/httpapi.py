"""Tower Remote's HTTP surface: a tiny, stdlib-only read API plus one
narrow, explicit write action.

Deliberately reuses the exact same modules the curses TUI uses for
discovery/status/activity/identity (a headless ``ui.tower.Tower``
instance, never re-implemented here) -- see docs/ARCHITECTURE.md. This
module only adds: JSON serialization of that data, pairing/token auth,
and the one allowed write action (typing an explicit, user-composed
prompt into one explicitly selected, still-live, non-dead pane).

Hard boundaries enforced here, not just documented:

* No endpoint accepts a raw shell command, a file path, or a pane_id with
  no corresponding live row -- ``/api/prompt`` re-resolves its target
  against a *fresh* pane snapshot on every call and rejects anything that
  doesn't match by key AND by the project/agent the client believes it's
  targeting (stale/wrong-pane rejection).
* ``/api/status`` never includes captured terminal content -- only the
  same small set of fields the TUI's row already carries (see
  ``build_status_payload``). Raw pane text exists in exactly one place,
  ``GET /api/panes/<key>/screen`` (the live view): paired token required,
  capture-pane only, hard line/byte caps, ``Cache-Control: no-store``,
  never written to disk or a log.
* Every request other than ``/api/health`` and ``/api/pair`` requires a
  valid bearer token (see ``auth.py``); a same-LAN attacker who hasn't
  seen the pairing code shown on the PC's own terminal gets nothing.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from .. import __version__
from ..control.actions import (
    MAX_IDENTITY_CHARS,
    MAX_PROMPT_CHARS,
    MAX_SCREEN_BYTES,
    MAX_SCREEN_LINE_CHARS,
    MAX_SCREEN_LINES,
    apply_identity_edit,
    build_screen_payload,
    find_prompt_target,
    find_row,
    find_screen_target,
    focus_pane,
    get_pane_screen,
    get_result,
    send_prompt,
    send_prompt_to_pane,
)
from ..detection.result import ResultTracker
from ..detection.status import StatusEngine
from ..tmux import capture as tmux_capture
from ..ui.tower import Tower
from . import auth
from .webui import PAGE_HTML

MAX_BODY_BYTES = 8 * 1024


def build_status_payload(tower: Tower) -> dict:
    """Minimal, JSON-safe snapshot of what the TUI would show right now.

    Never includes ``path`` (local filesystem layout) or any captured
    terminal content -- only display-level fields already meant to be
    shown to the user.
    """

    tower.load()
    panes: List[dict] = []

    for row in tower.rows:
        if row.get("placeholder"):
            panes.append(
                {
                    "key": row["key"],
                    "host": row["host"],
                    "offline": True,
                    "status": row.get("status"),
                    "remote": True,
                }
            )
            continue

        panes.append(
            {
                "key": row["key"],
                "host": row["host"],
                "project": row.get("project"),
                "agent": row.get("agent"),
                "auto_project": row.get("auto_project"),
                "auto_agent": row.get("auto_agent"),
                "pane_title": row.get("pane_title"),
                "status": row.get("status"),
                "result_state": row.get("result_state") or "none",
                "duration_seconds": round(row.get("duration_seconds") or 0.0, 1),
                "activity": row.get("activity_text"),
                "visit": row.get("visit"),
                "remote": bool(row.get("remote")),
                "offline": False,
                "session": row.get("session"),
                "window_index": row.get("window_index"),
                "window_name": row.get("window_name"),
                "pane_id": row.get("pane_id"),
                "pane_index": row.get("pane_index"),
                "active": bool(row.get("pane_active")),
            }
        )

    return {
        "ok": True,
        "panes": panes,
        "session": tower.session,
        "generated_at": time.time(),
        "version": __version__,
    }


def source_session_missing_payload(session: str) -> dict:
    """What the phone gets instead of a pane list once the tmux session
    this remote was bound to no longer exists. Deliberately no ``panes``
    key: configured SSH hosts must not show up alone and look like a
    healthy Tower with "no local panes".
    """

    return {"ok": False, "error": "source_session_missing", "session": session}


class TowerRemoteHandler(BaseHTTPRequestHandler):
    server_version = "TowerRemote/1"

    # -- helpers ------------------------------------------------------

    def _tower(self) -> Tower:
        return self.server.make_tower()  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict, no_store: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _pane_route(path: str) -> Optional[Tuple[str, str]]:
        """``/api/panes/<key>/<action>`` -> ``(key, action)``. The key is
        URL-encoded by the client (tmux ids contain ``%``)."""

        parts = path.split("?", 1)[0].split("/")
        if len(parts) != 5 or parts[1:3] != ["api", "panes"] or not parts[3]:
            return None
        return unquote(parts[3]), parts[4]

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_header_allowed(self) -> bool:
        """Defense-in-depth against DNS rebinding: the ``Host`` header
        must name one of the addresses/ports this server is actually
        bound to and printed to the user, not an arbitrary attacker
        domain that happens to resolve to this LAN IP.
        """

        allowed: List[str] = self.server.allowed_hosts  # type: ignore[attr-defined]
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        host_no_port = host.split(":", 1)[0]
        return host_no_port in allowed

    def _read_json_body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _bearer_token(self) -> Optional[str]:
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        return None

    def _require_auth(self) -> bool:
        token_store: auth.TokenStore = self.server.token_store  # type: ignore[attr-defined]
        if token_store.is_valid(self._bearer_token()):
            return True
        self._send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    # -- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib-mandated name)
        if not self._host_header_allowed():
            self._send_json(400, {"ok": False, "error": "invalid_host"})
            return

        if self.path == "/":
            self._send_html(200, PAGE_HTML)
            return
        if self.path == "/api/health":
            self._send_json(200, {"ok": True, "version": __version__})
            return
        if self.path == "/api/status":
            if not self._require_auth():
                return
            if not self.server.source_session_alive():  # type: ignore[attr-defined]
                self._send_json(503, source_session_missing_payload(self.server.session))  # type: ignore[attr-defined]
                return
            self._send_json(200, build_status_payload(self._tower()))
            return

        route = self._pane_route(self.path)
        if route and route[1] == "screen":
            self._get_screen(route[0])
            return
        if route and route[1] == "result":
            self._get_result(route[0])
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def _get_screen(self, pane_key: str) -> None:
        """Recent pane text for the live view. Read-only (``capture-pane``
        only), never written to disk or logged, ``Cache-Control: no-store``.
        This is the only endpoint that returns raw terminal text."""

        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        server = self.server
        ok, reason, payload = get_pane_screen(server.session, pane_key, server._own_pane_id)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, payload, no_store=True)

    def _get_result(self, pane_key: str) -> None:
        """Extracted final answer only. Does not mark it read -- opening
        the live pane, or this GET by itself, is not "the user looked at
        the result". Text stays in the process; ``no-store``.
        """

        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        ok, reason, payload = get_result(self._tower(), pane_key)
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, **payload}, no_store=True)

    def _post_result_read(self, pane_key: str) -> None:
        if not self._require_auth():
            return
        body = self._read_json_body()
        fingerprint = (body or {}).get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        ok = self.server.results.mark_read(pane_key, fingerprint)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(409, {"ok": False, "error": "stale_result"})
            return
        self._send_json(200, {"ok": True, "state": "read"})

    def _post_identity(self, pane_key: str) -> None:
        if not self._require_auth():
            return
        body = self._read_json_body()
        if not body:
            self._send_json(400, {"ok": False, "error": "bad_request"})
            return
        tower = self._tower()
        ok, reason = apply_identity_edit(tower, pane_key, body)
        if not ok:
            self._send_json(404 if reason == "not_found" else 400, {"ok": False, "error": reason})
            return
        row = find_row(tower, pane_key) or {}
        self._send_json(
            200,
            {
                "ok": True,
                "identity": {
                    "project": row.get("project"),
                    "agent": row.get("agent"),
                    "pane_title": row.get("pane_title"),
                    "auto_project": row.get("auto_project"),
                    "auto_agent": row.get("auto_agent"),
                },
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_header_allowed():
            self._send_json(400, {"ok": False, "error": "invalid_host"})
            return

        if self.path == "/api/pair":
            body = self._read_json_body()
            code = (body or {}).get("code") if body else None
            label = (body or {}).get("label") if body else None
            pairing: auth.PairingSession = self.server.pairing  # type: ignore[attr-defined]
            token = pairing.try_pair(
                str(code) if code is not None else "",
                label=label if isinstance(label, str) else None,
            )
            if token is None:
                self._send_json(400, {"ok": False, "error": "invalid_code"})
            else:
                self._send_json(200, {"ok": True, "token": token})
            return

        if self.path == "/api/prompt":
            if not self._require_auth():
                return

            body = self._read_json_body()
            if not body:
                self._send_json(400, {"ok": False, "error": "bad_request"})
                return

            pane_key = body.get("pane_key")
            text = body.get("text")
            expected_project = body.get("project")
            expected_agent = body.get("agent")

            if not isinstance(pane_key, str) or not pane_key:
                self._send_json(400, {"ok": False, "error": "missing_pane_key"})
                return
            if not isinstance(text, str) or not text:
                self._send_json(400, {"ok": False, "error": "missing_text"})
                return
            if len(text) > MAX_PROMPT_CHARS:
                self._send_json(413, {"ok": False, "error": "prompt_too_long"})
                return
            if not self.server.source_session_alive():  # type: ignore[attr-defined]
                self._send_json(409, source_session_missing_payload(self.server.session))  # type: ignore[attr-defined]
                return

            ok, reason = send_prompt(self._tower(), pane_key, text, expected_project, expected_agent)
            if not ok and reason == "send_failed":
                self._send_json(502, {"ok": False})
                return
            if not ok:
                self._send_json(409, {"ok": False, "error": reason})
                return
            self._send_json(200, {"ok": True})
            return

        route = self._pane_route(self.path)
        if route and route[1] == "identity":
            self._post_identity(route[0])
            return
        if route and route[1] == "result":
            self._post_result_read(route[0])
            return
        if route and route[1] == "focus":
            self._post_focus(route[0])
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

    def _post_focus(self, pane_key: str) -> None:
        """Move the attached tmux client to this pane. Not a prompt and
        not a keystroke: ``select-window`` + ``select-pane`` only, and
        only inside the session this remote was bound to.
        """

        if not self._require_auth():
            return
        if not self.server.source_session_alive():  # type: ignore[attr-defined]
            self._send_json(409, source_session_missing_payload(self.server.session), no_store=True)  # type: ignore[attr-defined]
            return
        server = self.server
        ok, reason = focus_pane(server.session, pane_key, server._own_pane_id)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        self._send_json(200, {"ok": True, "pane_id": pane_key}, no_store=True)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # Silence BaseHTTPRequestHandler's default stderr access log --
        # this runs in a user's foreground terminal via `tower serve`,
        # not a service with its own log pipeline.
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, session: str, own_pane_id: str, allowed_hosts: List[str], token_path):
        super().__init__(address, handler)
        self._session = session
        self._own_pane_id = own_pane_id
        self.allowed_hosts = allowed_hosts
        self.token_store = auth.TokenStore(token_path)
        self.pairing = auth.PairingSession(self.token_store)
        self._tower_lock = threading.Lock()
        # Shared across requests so status hysteresis and result
        # ready/read survive the next poll. Result text is memory only.
        self.status_engine = StatusEngine()
        self.results = ResultTracker()

    @property
    def session(self) -> str:
        return self._session

    def source_session_alive(self) -> bool:
        """Checked per request. The session is fixed at start; if it has
        been deleted since, every status/prompt call says so instead of
        returning an empty local list.
        """

        return tmux_capture.session_exists(self._session)

    def make_tower(self) -> Tower:
        # A fresh, headless Tower per request: cheap (no curses, no
        # persistent socket), and avoids sharing mutable UI-only state
        # (attention_mode, filter_text, selection) between concurrent
        # requests from possibly multiple paired devices.
        with self._tower_lock:
            return Tower(
                self._session,
                self._own_pane_id,
                status_engine=self.status_engine,
                results=self.results,
            )


def default_token_path():
    from pathlib import Path

    return Path.home() / ".config" / "tmux-agent-tower" / "remote-tokens.json"


def create_server(
    host: str,
    port: int,
    session: str,
    own_pane_id: str,
    allowed_hosts: List[str],
    token_path=None,
) -> _Server:
    """``allowed_hosts`` must be the exact set of hostnames/IPs the caller
    is about to print/QR to the user (e.g. ``["localhost", "127.0.0.1"]``,
    plus the detected LAN IP for ``--lan``) -- see ``TowerRemoteHandler``'s
    ``Host`` header check. Never pass a wildcard here.
    """

    return _Server(
        (host, port),
        TowerRemoteHandler,
        session,
        own_pane_id,
        list(allowed_hosts),
        token_path or default_token_path(),
    )
