"""One action layer for the Tower TUI and the phone remote.

View, prompt, identity, focus, result, and closing one pane all go
through here. Neither UI grows its own tmux calls for those actions.
Approval sends one key the adapter already named for the current widget.
Nothing here restarts a fleet, runs a shell, or guesses Enter or y.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..state.overrides import FIELDS as OVERRIDE_FIELDS
from ..tmux import capture as tmux_capture
from ..tmux.navigation import focus_local_pane

MAX_PROMPT_CHARS = 4000
SEND_TIMEOUT = 3.0
MAX_SCREEN_LINES = 60
MAX_SCREEN_LINE_CHARS = 400
MAX_SCREEN_BYTES = 16 * 1024
MAX_IDENTITY_CHARS = 80

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LOCAL_PANE_KEY_RE = re.compile(r"^%\d+$")


def enter_intent(row: Optional[dict]) -> str:
    """Enter opens the in-Tower control view. It never focuses a pane."""

    if not row or row.get("offline") or row.get("placeholder"):
        return "ignore"
    if row.get("kind") == "window" or row.get("pane_id") or row.get("key"):
        return "control"
    return "ignore"


def sanitize_screen_line(line: str) -> str:
    """Plain text only: drop ANSI and control bytes, clamp width."""

    text = _ANSI_RE.sub("", line)
    text = _CONTROL_RE.sub("", text)
    if len(text) > MAX_SCREEN_LINE_CHARS:
        text = text[: MAX_SCREEN_LINE_CHARS - 1] + "…"
    return text


def build_screen_payload(pane_key: str, raw_lines: List[str]) -> dict:
    """Recent screen text within hard limits. The tail is kept."""

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


def find_screen_target(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str, Optional[str]]:
    """``(ok, reason, tmux_pane_id)`` for a local pane in ``session``.

    Reasons: ``not_found``, ``remote_unsupported``, ``stale``.
    """

    if not _LOCAL_PANE_KEY_RE.match(pane_key or ""):
        return False, ("remote_unsupported" if pane_key and ":" in pane_key else "not_found"), None
    if own_pane_id and pane_key == own_pane_id:
        return False, "not_found", None
    listed = tmux_capture.run_tmux(["list-panes", "-s", "-t", session, "-F", "#{pane_id}"]).split("\n")
    if pane_key not in listed:
        return False, "not_found", None
    if not tmux_capture.pane_exists(pane_key):
        return False, "stale", None
    return True, "", pane_key


def get_pane_screen(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str, dict]:
    """Recent visible text of one local pane. ``capture-pane`` only."""

    ok, reason, pane_id = find_screen_target(session, pane_key, own_pane_id)
    if not ok or not pane_id:
        return False, reason or "not_found", {}
    raw = tmux_capture.capture_pane(pane_id, lines=MAX_SCREEN_LINES)
    return True, "", build_screen_payload(pane_key, raw)


def _clean_identity_value(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    if not text or len(text) > MAX_IDENTITY_CHARS:
        return None
    return text


def apply_identity_edit(tower, pane_key: str, body: dict) -> Tuple[bool, str]:
    """Display metadata only, on the same OverrideStore the TUI uses."""

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
            tmux_capture.run_tmux(["select-pane", "-t", row["pane_id"], "-T", cleaned], capture=False)

    if not touched:
        return False, "bad_request"
    return True, ""


def update_identity(tower, pane_key: str, body: dict) -> Tuple[bool, str]:
    return apply_identity_edit(tower, pane_key, body)


def find_row(tower, pane_key: str):
    tower.load()
    return next((r for r in tower.rows if r.get("key") == pane_key), None)


def find_prompt_target(
    tower, pane_key: str, expected_project: Optional[str], expected_agent: Optional[str]
) -> Tuple[bool, str, Optional[str]]:
    """Fresh snapshot. Refuses remote, dead, and project/agent mismatch."""

    tower.load()
    for row in tower.rows:
        if row.get("key") != pane_key or row.get("placeholder"):
            continue
        if row.get("remote"):
            return False, "remote_unsupported", None
        if row.get("status") == "DEAD":
            return False, "dead", None
        if expected_project is not None and row.get("project") != expected_project:
            return False, "mismatch", None
        if expected_agent is not None and row.get("agent") != expected_agent:
            return False, "mismatch", None
        return True, "", row.get("pane_id") or row.get("key")
    return False, "not_found", None


SUBMIT_CONFIRM_SECONDS = 3.0
SUBMIT_POLL_SECONDS = 0.25
_SAFE_SUBMIT_KEYS = frozenset({"Enter"})


@dataclass
class SubmitResult:
    """``ok`` means the text reached the pane. ``submitted`` means the
    agent was seen taking it as a new turn. They are reported separately
    so the phone never claims a submit it did not observe."""

    ok: bool
    submitted: bool = False
    reason: str = ""
    pane_id: str = ""

    def as_tuple(self) -> Tuple[bool, str]:
        return self.ok, self.reason


def send_text(pane_id: str, text: str) -> bool:
    """Deliver text as one bracketed paste.

    Measured live: ``send-keys -l`` followed by Enter is read by Codex as
    one paste burst and the Enter becomes a newline, so the prompt sat in
    the composer. ``load-buffer`` + ``paste-buffer -p`` tells the TUI
    where the paste ends. Korean, multiline, quotes, and code fences
    arrived intact in Codex, Claude Code, and OpenCode. ``shell=False``.
    """

    body = (text or "").rstrip("\n")
    if not body:
        return False
    name = f"tower-prompt-{os.getpid()}-{time.monotonic_ns()}"
    try:
        loaded = subprocess.run(
            ["tmux", "load-buffer", "-b", name, "-"],
            input=body.encode("utf-8"),
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        if loaded.returncode != 0:
            return False
        pasted = subprocess.run(
            ["tmux", "paste-buffer", "-d", "-p", "-b", name, "-t", pane_id],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return pasted.returncode == 0
    except Exception:
        subprocess.run(["tmux", "delete-buffer", "-b", name], capture_output=True, check=False)
        return False


def submit_input(pane_id: str, key: str = "Enter") -> bool:
    """Exactly one submit key. No retry lives here or in any caller."""

    if key not in _SAFE_SUBMIT_KEYS:
        return False
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, key],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def send_prompt_to_pane(pane_id: str, text: str, submit_key: str = "Enter") -> bool:
    """Paste once, then one submit key."""

    if not send_text(pane_id, text):
        return False
    return submit_input(pane_id, submit_key)


def _observe(pane_id: str):
    """Adapter and screen for one pane right now. None if it is gone."""

    from ..adapters import resolve_adapter
    from ..adapters.base import PaneContext

    raw = tmux_capture.run_tmux(
        ["list-panes", "-t", pane_id, "-F", "#{pane_title}\x1f#{pane_current_command}"]
    )
    if "\x1f" not in raw:
        return None
    title, command = raw.split("\x1f", 1)
    lines = tmux_capture.capture_pane(pane_id, lines=40)
    ctx = PaneContext(title=title, command=command, lines=tuple(lines))
    return resolve_adapter(command, title, ""), ctx


def confirm_submission(pane_id: str, adapter, before, text: str, timeout: Optional[float] = None) -> bool:
    """Watch the pane briefly for evidence the agent took the text.

    Nothing is sent from here. False means not confirmed, not failed.
    """

    deadline = time.monotonic() + (SUBMIT_CONFIRM_SECONDS if timeout is None else timeout)
    while True:
        fresh = _observe(pane_id)
        if fresh is not None:
            _adapter, after = fresh
            if adapter.confirm_submitted(before, after, text) is True:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SUBMIT_POLL_SECONDS)


def send_prompt(
    tower, pane_key: str, text: str, expected_project: Optional[str] = None, expected_agent: Optional[str] = None
) -> SubmitResult:
    """One explicit prompt into one still-live local pane, then a short
    before/after check. One paste, one Enter, no blind retry."""

    if not isinstance(text, str) or not text.strip():
        return SubmitResult(False, reason="missing_text")
    ok, reason, pane_id = find_prompt_target(tower, pane_key, expected_project, expected_agent)
    if not ok or not pane_id:
        return SubmitResult(False, reason=reason or "not_found")
    observed = _observe(pane_id)
    adapter, before = observed if observed is not None else (None, None)
    submit_key = adapter.submit_key(before) if adapter is not None else "Enter"
    if not send_prompt_to_pane(pane_id, text, submit_key):
        return SubmitResult(False, reason="send_failed", pane_id=pane_id)
    if adapter is None or before is None:
        return SubmitResult(True, submitted=False, reason="submit_not_confirmed", pane_id=pane_id)
    if confirm_submission(pane_id, adapter, before, text):
        return SubmitResult(True, submitted=True, pane_id=pane_id)
    return SubmitResult(True, submitted=False, reason="submit_not_confirmed", pane_id=pane_id)


def focus_pane(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str]:
    """``select-window`` + ``select-pane`` for one live local pane id."""

    ok, reason, pane_id = find_screen_target(session, pane_key, own_pane_id)
    if not ok or not pane_id:
        return False, reason or "not_found"
    if not focus_local_pane(session, pane_id):
        return False, "stale"
    return True, ""


def get_result(tower, pane_key: str) -> Tuple[bool, str, dict]:
    """Result state and, when ready or read, the extracted body.

    Opening this does not mark the result read.
    """

    row = find_row(tower, pane_key)
    if row is None or row.get("placeholder"):
        return False, "not_found", {}
    if row.get("remote"):
        return False, "remote_unsupported", {}
    tracker = getattr(tower, "results", None)
    if tracker is None:
        return True, "", {"state": row.get("result_state") or "none", "text": "", "fingerprint": "", "generated_at": 0.0}
    snap = tracker.snapshot(pane_key)
    text = snap.text if snap.state in ("ready", "read") else ""
    return True, "", {
        "state": snap.state,
        "text": text,
        "fingerprint": snap.fingerprint,
        "generated_at": snap.generated_at,
    }


_SAFE_ATTENTION_KEY = re.compile(r"^[0-9yn]$")
_SAFE_NAMED_KEYS = frozenset({"Enter", "Escape"})


def _attention_now(pane_id: str):
    """Fresh title, command, and screen for one pane. None if it is gone."""

    from ..adapters import resolve_adapter
    from ..adapters.base import PaneContext

    raw = tmux_capture.run_tmux(
        ["list-panes", "-t", pane_id, "-F", "#{pane_title}\x1f#{pane_current_command}"]
    )
    if "\x1f" not in raw:
        return None
    title, command = raw.split("\x1f", 1)
    lines = tmux_capture.capture_pane(pane_id, lines=40)
    ctx = PaneContext(title=title, command=command, lines=tuple(lines))
    adapter = resolve_adapter(command, title, "")
    return adapter, ctx


def send_attention_key(pane_id: str, key: str) -> bool:
    """Send one documented approval key. Not a prompt and not Enter."""

    if key not in _SAFE_NAMED_KEYS and not _SAFE_ATTENTION_KEY.match(key or ""):
        return False
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, key],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def respond_attention(tower, pane_key: str, action: str) -> Tuple[bool, str]:
    """Approve or reject only when the adapter names the key on a fresh screen."""

    if action not in ("approve", "reject"):
        return False, "bad_request"
    ok, reason, pane_id = find_prompt_target(tower, pane_key, None, None)
    if not ok or not pane_id:
        return False, reason or "not_found"
    fresh = _attention_now(pane_id)
    if fresh is None:
        return False, "stale"
    adapter, ctx = fresh
    if adapter.detect_attention(ctx) != "approval_required":
        return False, "not_approval"
    key = adapter.approve(ctx) if action == "approve" else adapter.reject(ctx)
    if not key:
        return False, "approval_unknown"
    if not send_attention_key(pane_id, key):
        return False, "send_failed"
    return True, ""


def close_warning(status: str) -> str:
    """Confirmation copy. The default choice is always cancel."""

    from ..i18n import t

    return {
        "WORKING": t("control.close_working"),
        "WAITING": t("control.close_waiting"),
        "DEAD": t("control.close_dead"),
    }.get(status or "", t("control.close_idle"))


def close_choices() -> List[Tuple[str, str]]:
    """Cancel is first so Enter without moving does not kill the pane."""

    from ..i18n import t

    return [("cancel", t("control.cancel")), ("close", t("control.close_yes"))]


def close_pane(session: str, pane_key: str, own_pane_id: str = "") -> Tuple[bool, str]:
    """``tmux kill-pane -t <pane_id>`` for one pane in the bound session.

    Tower's own pane is refused. Remote keys are refused. A stale id
    is refused. No window name, no session delete, no ``shell=True``.
    """

    if own_pane_id and pane_key == own_pane_id:
        return False, "tower_pane"
    ok, reason, pane_id = find_screen_target(session, pane_key, own_pane_id)
    if not ok or not pane_id:
        return False, reason or "not_found"
    try:
        result = subprocess.run(
            ["tmux", "kill-pane", "-t", pane_id],
            capture_output=True,
            timeout=SEND_TIMEOUT,
            check=False,
        )
    except Exception:
        return False, "close_failed"
    if result.returncode != 0:
        return False, "close_failed"
    return True, ""
