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

import json
import secrets
import time
from pathlib import Path
from typing import Optional

PAIRING_CODE_TTL_SECONDS = 300.0  # 5 minutes
_CODE_DIGITS = 6


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
            pass

    def is_valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        return token in self._load()

    def add_token(self, token: str) -> None:
        data = self._load()
        data[token] = {"paired_at": time.time()}
        self._tokens = data
        self._save()

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
    """

    def __init__(self, token_store: TokenStore, ttl_seconds: float = PAIRING_CODE_TTL_SECONDS):
        self.token_store = token_store
        self.ttl_seconds = ttl_seconds
        self._code: Optional[str] = None
        self._expires_at: float = 0.0

    def current_code(self, now: Optional[float] = None) -> str:
        """Returns the active pairing code, generating a new one if none
        is pending or the previous one expired.
        """

        now = time.monotonic() if now is None else now
        if self._code is None or now >= self._expires_at:
            self._code = _generate_code()
            self._expires_at = now + self.ttl_seconds
        return self._code

    def try_pair(self, submitted_code: str, now: Optional[float] = None) -> Optional[str]:
        """On a correct, still-valid code: issues and persists a new
        token, invalidates the code (one-time use), and returns the
        token. Returns ``None`` on any mismatch or expiry -- callers
        must not distinguish "wrong code" from "expired code" in the
        HTTP response, to avoid handing an attacker a timing oracle.
        """

        now = time.monotonic() if now is None else now

        if self._code is None or now >= self._expires_at:
            return None

        if not secrets.compare_digest(submitted_code or "", self._code):
            return None

        token = secrets.token_urlsafe(32)
        self.token_store.add_token(token)

        # One-time use: consumed immediately on success.
        self._code = None
        self._expires_at = 0.0

        return token
