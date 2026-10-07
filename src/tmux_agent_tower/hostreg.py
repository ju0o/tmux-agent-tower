"""Display names for the machine Tower is on and for SSH peers.

A host is one physical machine. Its hostname, SSH alias, and the label
in ``remote-hosts.txt`` must not become three rows. Nothing here stores
a resolved hostname in the repository. ``ssh -G`` results stay in memory.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Dict, List, Optional


def openssh_hostname(destination: str, timeout: float = 2.0) -> str:
    """Hostname OpenSSH would use for ``destination``. Empty on failure.

    ``ssh -G`` reads local config only. It does not open a connection.
    """

    token = (destination or "").strip()
    if not token:
        return ""
    try:
        result = subprocess.run(
            ["ssh", "-G", "-o", "BatchMode=yes", token],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    for line in (result.stdout or "").splitlines():
        if line.startswith("hostname "):
            return line.split(None, 1)[1].strip()
    return ""


class HostRegistry:
    """Map an SSH destination onto one display label."""

    def __init__(self, observer: str, raw_hostname: str, peers: Optional[List[Dict[str, str]]] = None):
        self.observer = observer or raw_hostname or ""
        self.raw_hostname = raw_hostname or ""
        self.peers = list(peers or [])
        self._resolved: Dict[str, str] = {}

    def local_aliases(self) -> set:
        names = set()
        for value in (self.observer, self.raw_hostname):
            text = (value or "").strip().lower()
            if text:
                names.add(text)
        return names

    def remember(self, token: str, hostname: str) -> None:
        key = (token or "").strip().lower()
        host = (hostname or "").strip().lower()
        if key and host:
            self._resolved[key] = host

    def display_for(self, token: str, resolved_hostname: str = "") -> str:
        """One label for this destination. Unknown tokens stay as typed."""

        typed = (token or "").strip()
        host = typed.lower()
        resolved = (resolved_hostname or self._resolved.get(host, "")).strip().lower()
        local = self.local_aliases()
        if host in local or (resolved and resolved in local):
            return self.observer
        for peer in self.peers:
            alias = (peer.get("alias") or "").strip()
            name = (peer.get("name") or alias).strip()
            names = {alias.lower(), name.lower()}
            remembered = self._resolved.get(alias.lower(), "")
            if host in names or (resolved and resolved in names):
                return name
            if resolved and remembered and resolved == remembered:
                return name
            if host and remembered and host == remembered:
                return name
        return typed

    def resolve(self, token: str, runner: Optional[Callable[[str], str]] = None) -> str:
        """Resolve once per destination, then reuse the memory cache."""

        typed = (token or "").strip()
        if not typed:
            return ""
        key = typed.lower()
        if key not in self._resolved and runner is not None:
            hostname = (runner(typed) or "").strip()
            if hostname:
                self._resolved[key] = hostname.lower()
        return self.display_for(typed, self._resolved.get(key, ""))
