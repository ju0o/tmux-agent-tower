"""Best-effort LAN IP detection for printing a connect URL.

Uses the standard "open a UDP socket toward a public address" trick to
ask the OS's routing table which local interface it would use -- no
packet is actually sent (UDP `connect()` just picks a route), so this
works fully offline too and needs no network access of its own.
"""

from __future__ import annotations

import socket
from typing import Optional


def detect_lan_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None
