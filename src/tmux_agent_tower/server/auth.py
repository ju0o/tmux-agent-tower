"""Pairing + token auth for Tower Remote.

Tower Remote is LAN-reachable (optionally, via ``tower serve --lan``), so
being on the same network is never treated as authorization by itself --
every API call except ``/api/health`` and the pairing exchange itself
requires a bearer token, obtained once via a short-lived, single-use
6-digit pairing code shown on the PC's own terminal (never sent over the
network to anyone who doesn't already have it).

Tokens are opaque random strings (``secrets.token_urlsafe``), persisted
locally so a phone stays paired across ``tower serve`` restarts, and
never committed to git (state lives under the user's config directory,
same as everything else in ``state/``).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import List, Optional

PAIRING_CODE_TTL_SECONDS = 300.0  # 5 minutes
MAX_PAIR_ATTEMPTS = 7
_CODE_DIGITS = 6


def _secure_file(path: Path) -> None:
    """Best-effort ``chmod 0600`` -- ``remote-tokens.json`` holds bearer
    credentials. Never raises: a non-POSIX filesystem (Windows) either
    ignores this or fails in a way we don't want to take the whole
    process down over, and a locked-down environment where chmod itself
    is refused shouldn't break pairing either.
    """

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _secure_dir(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRWXU)
    except Exception:
        pass


def _clean_label(label: Optional[str]) -> str:
    if not isinstance(label, str):
        return ""
    cleaned = "".join(ch for ch in label.strip() if ch.isprintable() and ch not in "\r\n\t")
    return cleaned[:40]


def _device_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _generate_code() -> str:
    # A random 6-digit code, zero-padded -- e.g. "004821", not restricted
    # to a "nice-looking" 100000-999999 range, since padding is trivial
    # and it doesn't need to look like a phone number.
    return f"{secrets.randbelow(10 ** _CODE_DIGITS):0{_CODE_DIGITS}d}"


class TokenStore:
    """Persisted set of paired-device tokens.

    Malformed/missing state fails safe to "no tokens" (nobody is paired)
    rather than raising -- consistent with every other state file in this
    project (see ``state/overrides.py``).
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _secure_dir(self.path.parent)
        if self.path.exists():
            # An existing file from before this hardening pass (or one
            # that somehow ended up world/group-readable) gets tightened
            # proactively, not just newly-written ones.
            _secure_file(self.path)
        self._tokens: Optional[dict] = None

    def _load(self) -> dict:
        if self._tokens is not None:
            return self._tokens

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except Exception:
            data = {}

        if not isinstance(data, dict):
            data = {}

        self._tokens = data
        return self._tokens

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._tokens, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            return
        _secure_file(self.path)

    def is_valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        return token in self._load()

    def add_token(self, token: str, label: Optional[str] = None) -> None:
        data = self._load()
        entry = {"paired_at": time.time()}
        cleaned = _clean_label(label)
        if cleaned:
            entry["label"] = cleaned
        data[token] = entry
        self._tokens = data
        self._save()

    def list_devices(self) -> List[dict]:
        """Paired devices for the local TUI. The bearer token itself is
        never included -- ``id`` is a hash the TUI can hand back to
        ``revoke_id`` without displaying the credential.
        """

        devices = []
        for token, meta in self._load().items():
            if not isinstance(meta, dict):
                meta = {}
            devices.append({
                "id": _device_id(token),
                "label": meta.get("label") or "",
                "paired_at": meta.get("paired_at"),
            })
        devices.sort(key=lambda item: item.get("paired_at") or 0, reverse=True)
        return devices

    def revoke_id(self, device_id: str) -> bool:
        data = self._load()
        for token in list(data):
            if _device_id(token) == device_id:
                del data[token]
                self._tokens = data
                self._save()
                return True
        return False

    def revoke_all(self) -> None:
        """Used by tests and by an explicit future "forget all devices"
        action; never called automatically by the pairing flow itself.
        """

        self._tokens = {}
        self._save()


class PairingSession:
    """One pending pairing code at a time -- printed on the PC's own
    terminal by ``tower serve``, never transmitted anywhere else. A
    fresh code is generated on demand (server startup, or after the
    previous one is used/expired) and is single-use: a successful pair
    immediately invalidates it so the same code can't be replayed.

    Also rate-limited: ``max_attempts`` wrong guesses against one code
    invalidates that code immediately, regardless of how much of its TTL
    is left. A LAN attacker who can't see the code has no way to brute
    force a 6-digit space at more than a handful of guesses. The only way
    to get a *new* code after a lockout is ``regenerate()`` -- called
    locally by ``tower serve``'s own process (a terminal keypress), never
    reachable over HTTP (see ``httpapi.py``: no endpoint calls it).
    """

    def __init__(
        self,
        token_store: TokenStore,
        ttl_seconds: float = PAIRING_CODE_TTL_SECONDS,
        max_attempts: int = MAX_PAIR_ATTEMPTS,
    ):
        self.token_store = token_store
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self._code: Optional[str] = None
        self._expires_at: float = 0.0
        self._attempts: int = 0

    def current_code(self, now: Optional[float] = None) -> str:
        """Returns the active pairing code, generating a new one if none
        is pending or the previous one expired (or was locked out).
        """

        now = time.monotonic() if now is None else now
        if self._code is None or now >= self._expires_at:
            self._new_code(now)
        return self._code

    def snapshot(self) -> dict:
        """``{"code", "expires_at", "expired"}`` for a local status file.

        ``expires_at`` is wall-clock ``time.time()`` (so another process
        can display a countdown). ``expired`` is true when there is no
        code pending -- used up, locked out, or never issued.
        """

        now_mono = time.monotonic()
        if self._code is None or now_mono >= self._expires_at:
            return {"code": None, "expires_at": None, "expired": True}
        remaining = self._expires_at - now_mono
        return {"code": self._code, "expires_at": time.time() + remaining, "expired": False}

    def regenerate(self, now: Optional[float] = None) -> str:
        """Unconditionally issues a brand-new code (and resets the
        attempt counter), even if the current one is still valid --
        local-only, e.g. a terminal keypress. Immediately invalidates
        whatever code was previously shown.
        """

        now = time.monotonic() if now is None else now
        self._new_code(now)
        return self._code

    def _new_code(self, now: float) -> None:
        self._code = _generate_code()
        self._expires_at = now + self.ttl_seconds
        self._attempts = 0

    def try_pair(self, submitted_code: str, now: Optional[float] = None, label: Optional[str] = None) -> Optional[str]:
        """On a correct, still-valid, not-locked-out code: issues and
        persists a new token, invalidates the code (one-time use), and
        returns the token. Returns ``None`` on any mismatch, expiry, or
        lockout -- callers must not distinguish these cases in the HTTP
        response (no "wrong" vs "expired" vs "locked" detail), to avoid
        handing an attacker a timing or information oracle.
        """

        now = time.monotonic() if now is None else now

        if self._code is None or now >= self._expires_at:
            return None

        if self._attempts >= self.max_attempts:
            # Already locked out from a previous run of guesses -- make
            # sure it stays invalidated even if someone calls try_pair
            # again with the right code by coincidence.
            self._code = None
            self._expires_at = 0.0
            return None

        if not secrets.compare_digest(submitted_code or "", self._code):
            self._attempts += 1
            if self._attempts >= self.max_attempts:
                # Exceeded the budget: invalidate immediately rather than
                # waiting for TTL expiry, and require an explicit local
                # regenerate() for a new code.
                self._code = None
                self._expires_at = 0.0
            return None

        token = secrets.token_urlsafe(32)
        self.token_store.add_token(token, label=label)

        # One-time use: consumed immediately on success.
        self._code = None
        self._expires_at = 0.0
        self._attempts = 0

        return token
