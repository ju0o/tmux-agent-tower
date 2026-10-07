"""The user device operating Tower, kept separate from Tower and Agent hosts.

Only non-secret routing metadata crosses SSH. Clipboard bridge credentials
remain in the bridge's private registration file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import platform as _platform
import socket
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

ACCESS_CONTEXT_ENV = "TOWER_ACCESS_CONTEXT_V1"
_MAX_CONTEXT_BYTES = 2048
_PLATFORMS = {"linux-wayland", "linux-x11", "linux", "windows-wsl", "windows", "darwin", "unknown"}
_TRANSPORTS = {"local", "ssh", "unknown"}
_CAPABILITIES = {"bridge", "host", "browser", "none", "unknown"}
_LIVENESS = {"live", "stale", "unknown"}


@dataclass(frozen=True)
class AccessContext:
    client_id: str
    display_name: str
    platform: str
    transport: str
    access_host: str
    tower_host: str
    clipboard_capability: str
    registration_timestamp: float
    liveness: str

    def __post_init__(self) -> None:
        if not self.client_id or len(self.client_id) > 64:
            raise ValueError("invalid access client id")
        for name in ("display_name", "access_host", "tower_host"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 128 or any(not ch.isprintable() for ch in value):
                raise ValueError(f"invalid {name}")
        if self.platform not in _PLATFORMS or self.transport not in _TRANSPORTS:
            raise ValueError("invalid access context platform or transport")
        if self.clipboard_capability not in _CAPABILITIES or self.liveness not in _LIVENESS:
            raise ValueError("invalid access context capability or liveness")
        if isinstance(self.registration_timestamp, bool) or not math.isfinite(self.registration_timestamp):
            raise ValueError("invalid access context timestamp")


def encode_access_context(context: AccessContext) -> str:
    """Compact URL-safe value suitable for one SSH client environment entry."""

    payload = json.dumps(asdict(context), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(payload) > _MAX_CONTEXT_BYTES:
        raise ValueError("access context too large")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_access_context(value: str) -> Optional[AccessContext]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        encoded = value.encode("ascii")
        payload = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        if len(payload) > _MAX_CONTEXT_BYTES:
            return None
        raw = json.loads(payload.decode("ascii"))
        if not isinstance(raw, dict) or set(raw) != {
            "client_id", "display_name", "platform", "transport", "access_host", "tower_host",
            "clipboard_capability", "registration_timestamp", "liveness",
        }:
            return None
        return AccessContext(**raw)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def context_from_dict(value: object) -> Optional[AccessContext]:
    """Validate the metadata carried alongside a bridge registration."""
    if not isinstance(value, dict):
        return None
    try:
        fields = {
            "client_id", "display_name", "platform", "transport", "access_host", "tower_host",
            "clipboard_capability", "registration_timestamp", "liveness",
        }
        if set(value) != fields:
            return None
        return AccessContext(**value)
    except (ValueError, TypeError):
        return None


def context_from_environment(raw: bytes) -> Optional[AccessContext]:
    """Extract only the non-secret context key from a process environment."""

    prefix = f"{ACCESS_CONTEXT_ENV}=".encode("ascii")
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            try:
                value = entry[len(prefix):].decode("ascii")
            except UnicodeDecodeError:
                return None
            return decode_access_context(value)
    return None


def context_for_process(pid: int) -> Optional[AccessContext]:
    try:
        return context_from_environment(Path(f"/proc/{int(pid)}/environ").read_bytes())
    except (OSError, ValueError):
        return None


def discover_wayland_display(environ: Optional[Mapping[str, str]] = None) -> Optional[tuple[str, str]]:
    """Find a Wayland socket only when this user has exactly one candidate."""

    env = os.environ if environ is None else environ
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    try:
        sockets = sorted(path for path in Path(runtime).glob("wayland-*") if path.is_socket())
    except OSError:
        return None
    return (runtime, sockets[0].name) if len(sockets) == 1 else None


def platform_name(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        release = str(env.get("TOWER_UNAME", "") or _platform.uname().release).lower()
        if "microsoft" in release or "wsl" in release:
            return "windows-wsl"
        if env.get("WAYLAND_DISPLAY") or discover_wayland_display(env):
            return "linux-wayland"
        if env.get("DISPLAY"):
            return "linux-x11"
        return "linux"
    return "unknown"


def local_access_context(
    tty: str,
    pid: int,
    *,
    tower_host: str = "",
    clipboard_capability: str = "unknown",
    now: Optional[float] = None,
) -> AccessContext:
    host = socket.gethostname() or "UNKNOWN"
    identity = hashlib.sha256(f"{host}\0{tty}\0{pid}".encode("utf-8", "replace")).hexdigest()[:32]
    return AccessContext(
        client_id=identity,
        display_name=host,
        platform=platform_name(),
        transport="local",
        access_host=host,
        tower_host=tower_host or host,
        clipboard_capability=clipboard_capability,
        registration_timestamp=time.time() if now is None else now,
        liveness="live",
    )


def at_tower(context: AccessContext, tower_host: str, *, liveness: str = "live") -> AccessContext:
    """Set the current Tower host without replacing the originating client."""

    if context.tower_host == tower_host and context.liveness == liveness:
        return context
    return replace(context, tower_host=tower_host or "UNKNOWN", liveness=liveness)
