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
  ``build_status_payload``).
* Every request other than ``/api/health`` and ``/api/pair`` requires a
  valid bearer token (see ``auth.py``); a same-LAN attacker who hasn't
  seen the pairing code shown on the PC's own terminal gets nothing.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

from .. import __version__
from ..ui.tower import Tower
from . import auth
from .webui import PAGE_HTML

MAX_BODY_BYTES = 8 * 1024
MAX_PROMPT_CHARS = 4000
SEND_TIMEOUT = 3.0


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
                "pane_title": row.get("pane_title"),
                "status": row.get("status"),
                "duration_seconds": round(row.get("duration_seconds") or 0.0, 1),
                "activity": row.get("activity_text"),
                "visit": row.get("visit"),
                "remote": bool(row.get("remote")),
                "offline": False,
            }
        )

    return {"panes": panes, "generated_at": time.time(), "version": __version__}


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

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        elif self.path == "/api/health":
            self._send_json(200, {"ok": True, "version": __version__})
        elif self.path == "/api/status":
            if not self._require_auth():
                return
            self._send_json(200, build_status_payload(self._tower()))
        else:
            self._send_json(404, {"ok": False, "error": "not_found"})

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

            ok, reason, pane_id = find_prompt_target(self._tower(), pane_key, expected_project, expected_agent)
            if not ok:
                self._send_json(409, {"ok": False, "error": reason})
                return

            sent = send_prompt_to_pane(pane_id, text)
            self._send_json(200 if sent else 502, {"ok": sent})
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
