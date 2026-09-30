"""Windows-host Tailscale Serve integration for ``tower serve --tailscale``.

Architecture (verified live against a real Tailscale 1.102.2 install --
see the exact commands and output this module was built against in the
branch's commit history, not guessed from documentation):

    Phone --Tailscale--> Windows Tailscale Serve (HTTPS, MagicDNS name)
                              |
                          Windows 127.0.0.1:<port>  (WSL2 localhost
                              |                      forwarding -- no
                          WSL Tower Remote backend    extra setup needed)

No Tailscale daemon is started inside WSL -- this module only ever
shells out to the *Windows* ``tailscale.exe`` (Tailscale's own docs
advise against running both a Windows and a WSL-native Tailscale
instance at once). The Tower Remote backend itself always binds
``127.0.0.1`` only in this mode; ``tailscale serve`` is what makes it
tailnet-reachable, not a wider bind.

Hard rules enforced here, not just documented:

* Never calls ``tailscale funnel`` (that would expose the backend to the
  public internet -- this project has no auth model strong enough for
  that, see docs/REMOTE.md).
* Never calls ``tailscale serve reset`` (that would destroy any serve
  mapping the user already had for something unrelated).
* Before creating a mapping, checks whether one already exists at the
  target port and refuses if it points anywhere other than our own
  backend -- never silently overwrites an existing mapping.
* Before removing a mapping on shutdown, re-checks that it still points
  at our own backend -- never removes a mapping that changed underneath
  us (edited by the user or another process in the meantime).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

TIMEOUT = 5.0
DEFAULT_HTTPS_PORT = 443

# The one place this project expects to find Tailscale on a WSL2 setup:
# the Windows install, reached via the /mnt/c interop path. Deliberately
# not a WSL-native Tailscale install -- see module docstring.
_WINDOWS_TAILSCALE_PATH = "/mnt/c/Program Files/Tailscale/tailscale.exe"


def find_tailscale_exe() -> Optional[str]:
    """Windows Tailscale first (the supported WSL2 path), falling back to
    a native ``tailscale``/``tailscale.exe`` on PATH for non-WSL setups
    (native Linux/macOS). Returns ``None`` if neither is found -- callers
    must fail closed, never guess a path.
    """

    if Path(_WINDOWS_TAILSCALE_PATH).exists():
        return _WINDOWS_TAILSCALE_PATH
    return shutil.which("tailscale.exe") or shutil.which("tailscale")


def _run(exe: str, args, timeout: float = TIMEOUT) -> Optional[str]:
    try:
        result = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def get_status(exe: str) -> Optional[dict]:
    out = _run(exe, ["status", "--json"])
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def self_dns_name(status: dict) -> Optional[str]:
    """The tailnet MagicDNS hostname for this machine (e.g.
    ``mybox.tail1234.ts.net``), or ``None`` if MagicDNS is
    disabled for this tailnet -- in which case ``tailscale serve``'s
    HTTPS mode has no name to issue a certificate for, and this module's
    primary path can't be used (see docs/REMOTE.md's Tailscale section
    for the documented, not-yet-implemented fallback).
    """

    name = (status.get("Self") or {}).get("DNSName") or ""
    return name.rstrip(".") or None


def self_tailscale_ip(status: dict) -> Optional[str]:
    """The IPv4 tailnet address, for display as a debug/fallback
    reference only -- never used as a bind address (see module
    docstring: the backend stays on 127.0.0.1).
    """

    for ip in (status.get("Self") or {}).get("TailscaleIPs") or []:
        if ":" not in ip:
            return ip
    return None


def get_serve_status(exe: str) -> Optional[dict]:
    out = _run(exe, ["serve", "status", "--json"])
    if out is None:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def existing_mapping_proxy(serve_status: Optional[dict], dns_name: str, https_port: int = DEFAULT_HTTPS_PORT) -> Optional[str]:
    """The current proxy target configured for ``<dns_name>:<https_port>``
    (e.g. ``"http://127.0.0.1:4312"``), or ``None`` if nothing is
    configured there yet.
    """

    if not serve_status:
        return None
    web = serve_status.get("Web") or {}
    entry = web.get(f"{dns_name}:{https_port}")
    if not entry:
        return None
    return ((entry.get("Handlers") or {}).get("/") or {}).get("Proxy")


def start_serve(exe: str, backend_port: int, https_port: int = DEFAULT_HTTPS_PORT) -> Tuple[bool, str]:
    """Returns ``(ok, detail)``. On success, ``detail`` is the MagicDNS
    hostname to show the user. On failure, ``detail`` is a short reason
    code: ``"tailscale_status_unavailable"``, ``"no_magicdns"``,
    ``"existing_mapping:<what's already there>"``, or
    ``"serve_command_failed"``.
    """

    status = get_status(exe)
    if not status:
        return False, "tailscale_status_unavailable"

    dns_name = self_dns_name(status)
    if not dns_name:
        return False, "no_magicdns"

    our_target = f"http://127.0.0.1:{backend_port}"
    existing = existing_mapping_proxy(get_serve_status(exe), dns_name, https_port)

    if existing == our_target:
        return True, dns_name  # already set up correctly (e.g. re-run)

    if existing is not None:
        return False, f"existing_mapping:{existing}"

    if _run(exe, ["serve", "--bg", str(backend_port)]) is None:
        return False, "serve_command_failed"

    return True, dns_name


def stop_serve(exe: str, backend_port: int, https_port: int = DEFAULT_HTTPS_PORT) -> bool:
    """Removes the mapping *only* if it still points at our own backend.
    Uses the exact per-port ``off`` form Tailscale's own CLI prints after
    creating a mapping (``tailscale serve --https=<port> off``) -- never
    ``serve reset``, which would remove every mapping regardless of who
    created it.
    """

    status = get_status(exe)
    if not status:
        return False

    dns_name = self_dns_name(status)
    if not dns_name:
        return False

    our_target = f"http://127.0.0.1:{backend_port}"
    existing = existing_mapping_proxy(get_serve_status(exe), dns_name, https_port)

    if existing != our_target:
        # Not ours (anymore) -- someone else changed it, or it was never
        # ours to begin with. Leave it alone.
        return False

    return _run(exe, ["serve", f"--https={https_port}", "off"]) is not None


def windows_can_reach_backend(port: int, path: str = "/api/health") -> Optional[bool]:
    """Uses Windows's own ``curl.exe`` (reached via the WSL /mnt/c
    interop path) to verify that Windows can actually reach the WSL
    backend at ``127.0.0.1:<port>`` -- this is exactly the path
    ``tailscale serve`` (a Windows-side daemon) would use, so a failure
    here means Serve would silently not work even if every other step
    succeeded. Returns ``None`` (not ``False``) if ``curl.exe`` itself
    isn't available -- callers must treat that as "couldn't verify," not
    "verified broken."
    """

    curl = shutil.which("curl.exe")
    if not curl:
        return None

    try:
        result = subprocess.run(
            [curl, "-s", "-o", "NUL", "-w", "%{http_code}", f"http://127.0.0.1:{port}{path}"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except Exception:
        return False

    return result.stdout.strip() == "200"
