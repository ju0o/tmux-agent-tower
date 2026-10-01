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
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from .. import __version__
from ..state.overrides import FIELDS as OVERRIDE_FIELDS
from ..tmux import capture as tmux_capture
from ..ui.tower import Tower
from . import auth
from .webui import PAGE_HTML

MAX_BODY_BYTES = 8 * 1024
MAX_PROMPT_CHARS = 4000
SEND_TIMEOUT = 3.0

# Live pane view (GET /api/panes/<key>/screen): the *recent screen*, not
# the scrollback. Small on purpose -- this is polled once a second from a
# phone, so payload size is the whole feature's cost.
MAX_SCREEN_LINES = 60
MAX_SCREEN_LINE_CHARS = 400
MAX_SCREEN_BYTES = 16 * 1024
MAX_IDENTITY_CHARS = 80

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b[@-Z\\-_]"  # two-byte escapes
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_screen_line(line: str) -> str:
    """Plain text only: drop ANSI escapes and control bytes (keep tabs),
    clamp width. Colour is not preserved in V0 -- readable beats pretty.
    """

    text = _ANSI_RE.sub("", line)
    text = _CONTROL_RE.sub("", text)
    if len(text) > MAX_SCREEN_LINE_CHARS:
        text = text[: MAX_SCREEN_LINE_CHARS - 1] + "…"
    return text


def build_screen_payload(pane_key: str, raw_lines: List[str]) -> dict:
    """Recent screen text within hard limits. Trailing blank lines (an
    idle pane's empty bottom half) are dropped; then the *last*
    ``MAX_SCREEN_LINES`` lines are kept, and the byte budget is enforced
    by dropping from the top so the prompt line always survives.
    """

    lines = [sanitize_screen_line(line) for line in raw_lines]
    while lines and not lines[-1].strip():
        lines.pop()
    truncated = len(lines) > MAX_SCREEN_LINES
    lines = lines[-MAX_SCREEN_LINES:]

    def size(items: List[str]) -> int:
        return sum(len(item.encode("utf-8")) + 1 for item in items)

    while lines and size(lines) > MAX_SCREEN_BYTES:
        lines.pop(0)
        truncated = True

    return {
        "ok": True,
        "pane_key": pane_key,
        "lines": lines,
        "truncated": truncated,
        "generated_at": time.time(),
    }


_LOCAL_PANE_KEY_RE = re.compile(r"^%\d+$")


def find_screen_target(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str, Optional[str]]:
    """``(ok, reason, tmux_pane_id)`` for the live view. Reasons:
    ``not_found``, ``remote_unsupported`` (SSH hosts are title-only, see
    docs/ARCHITECTURE.md -- there is no remote capture to show), or
    ``stale`` when the pane was listed but is already gone.

    Deliberately does *not* build a ``Tower`` snapshot: that would capture
    every pane and poll the SSH hosts once per phone poll. Two cheap tmux
    calls are enough -- and the membership check keeps the endpoint from
    ever reading a pane outside the bound session.
    """

    if not _LOCAL_PANE_KEY_RE.match(pane_key):
        # Remote rows are keyed ``<alias>:<pane_id>`` / ``remote:<alias>:...``.
        return False, ("remote_unsupported" if ":" in pane_key else "not_found"), None
    if own_pane_id and pane_key == own_pane_id:
        return False, "not_found", None
    listed = tmux_capture.run_tmux(["list-panes", "-s", "-t", session, "-F", "#{pane_id}"]).split("\n")
    if pane_key not in listed:
        return False, "not_found", None
    if not tmux_capture.pane_exists(pane_key):
        return False, "stale", None
    return True, "", pane_key


def _clean_identity_value(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    if not text or len(text) > MAX_IDENTITY_CHARS:
        return None
    return text


def apply_identity_edit(tower: Tower, pane_key: str, body: dict) -> Tuple[bool, str]:
    """The phone's "편집": exactly what the TUI's ``E`` menu does, on the
    same ``OverrideStore`` file, so the PC picks it up on its next refresh.

    Body: ``{"reset": true}`` clears every override for the pane;
    otherwise any of ``project`` / ``agent`` / ``title`` set to a string
    (set) or ``null`` (back to auto-detected). Display metadata only --
    nothing here touches the process in the pane.
    """

    tower.load()
    row = next((r for r in tower.rows if r.get("key") == pane_key and not r.get("placeholder")), None)
    if row is None:
        return False, "not_found"

    if body.get("reset") is True:
        tower.overrides.reset(pane_key)
        return True, ""

    touched = False
    for field in OVERRIDE_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if value is None:
            tower.overrides.clear_field(pane_key, field)
            touched = True
            continue
        cleaned = _clean_identity_value(value)
        if cleaned is None:
            return False, f"invalid_{field}"
        getattr(tower.overrides, f"set_{field}")(pane_key, cleaned)
        touched = True
        if field == "title" and not row.get("remote"):
            # Same best-effort push the TUI does: the real tmux pane title
            # changes too, so the two views never disagree.
            tmux_capture.run_tmux(["select-pane", "-t", row["pane_id"], "-T", cleaned], capture=False)

    if not touched:
        return False, "bad_request"
    return True, ""


def find_row(tower: Tower, pane_key: str) -> Optional[dict]:
    tower.load()
    return next((r for r in tower.rows if r.get("key") == pane_key), None)


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
                "duration_seconds": round(row.get("duration_seconds") or 0.0, 1),
                "activity": row.get("activity_text"),
                "visit": row.get("visit"),
                "remote": bool(row.get("remote")),
                "offline": False,
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


def find_prompt_target(
    tower: Tower, pane_key: str, expected_project: Optional[str], expected_agent: Optional[str]
) -> Tuple[bool, str, Optional[str]]:
    """Re-resolves ``pane_key`` against a *fresh* pane snapshot (never a
    cached one) and returns ``(ok, reason, tmux_pane_id)``.

    ``reason`` is one of: "not_found", "dead", "remote_unsupported",
    "mismatch", or "" on success. The mismatch check exists because a
    pane_id can be reused after a tmux server restart (see
    ``state/overrides.py``'s module docstring) -- if the project/agent the
    mobile client last saw no longer matches what's actually at that key
    right now, refuse rather than silently sending the prompt to an
    unrelated pane.
    """

    tower.load()

    for row in tower.rows:
        if row.get("key") != pane_key or row.get("placeholder"):
            continue

        if row.get("remote"):
            return False, "remote_unsupported", None

        status = row.get("status")
        if status == "DEAD":
            return False, "dead", None

        if expected_project is not None and row.get("project") != expected_project:
            return False, "mismatch", None
        if expected_agent is not None and row.get("agent") != expected_agent:
            return False, "mismatch", None

        return True, "", row.get("pane_id") or row.get("key")

    return False, "not_found", None


def send_prompt_to_pane(pane_id: str, text: str) -> bool:
    """``tmux send-keys -l <text>`` (literal -- never interpreted as tmux
    key names) followed by a separate Enter, matching what a person
    typing directly into that pane would produce. Both calls run with
    ``shell=False`` and a fixed argument list, so the prompt text can
    never escape into a host shell command regardless of its content.
    """

    try:
        literal = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "-l", text],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        if literal.returncode != 0:
            return False

        enter = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "Enter"],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return enter.returncode == 0
    except Exception:
        return False


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
        ok, reason, pane_id = find_screen_target(server.session, pane_key, server._own_pane_id)  # type: ignore[attr-defined]
        if not ok:
            self._send_json(404 if reason == "not_found" else 409, {"ok": False, "error": reason}, no_store=True)
            return
        raw = tmux_capture.capture_pane(pane_id, lines=MAX_SCREEN_LINES)
        self._send_json(200, build_screen_payload(pane_key, raw), no_store=True)

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

            ok, reason, pane_id = find_prompt_target(self._tower(), pane_key, expected_project, expected_agent)
            if not ok:
                self._send_json(409, {"ok": False, "error": reason})
                return

            sent = send_prompt_to_pane(pane_id, text)
            self._send_json(200 if sent else 502, {"ok": sent})
            return

        route = self._pane_route(self.path)
        if route and route[1] == "identity":
            self._post_identity(route[0])
            return

        self._send_json(404, {"ok": False, "error": "not_found"})

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
            return Tower(self._session, self._own_pane_id)


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
