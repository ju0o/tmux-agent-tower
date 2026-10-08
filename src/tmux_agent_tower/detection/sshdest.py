"""Find an ssh client in a pane's process tree and read its destination.

The destination is an argv token, not a word inside a shell string.
``bash -c 'echo ssh remote-host'`` is not an ssh client. A wrapper is only
an ssh client when a process whose program is ``ssh`` is actually in
the tree.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# OpenSSH options that consume the next argument when it is not attached.
_WITH_ARG = set("bcDEeFIiJLlmoOpQRSw")


def program_name(argv0: str) -> str:
    """Basename of an executable, without a login-shell dash."""

    return Path(argv0 or "").name.lower().lstrip("-")


def split_command(args: str) -> List[str]:
    text = (args or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def host_from_destination(token: str) -> str:
    """``user@host`` and ``[ipv6]`` become the host token. Empty if unsafe."""

    text = (token or "").strip()
    if not text or text.startswith("-") or any(ch.isspace() for ch in text):
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    if not text or text.startswith("<") or "defunct" in text.lower():
        return ""
    return text


def destination_from_argv(argv: List[str]) -> str:
    """The host positional of an ssh argv, or empty when it is not one.

    ``ssh -J jump -p 22 -t user@host remote-cmd`` yields ``host``.
    The jump host and the remote command are not the destination.
    """

    if not argv or program_name(argv[0]) != "ssh":
        return ""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            index += 1
            break
        if arg.startswith("-") and arg != "-":
            letters = arg[1:]
            cursor = 0
            while cursor < len(letters):
                letter = letters[cursor]
                if letter in _WITH_ARG:
                    rest = letters[cursor + 1:]
                    if not rest:
                        index += 1
                    break
                cursor += 1
            index += 1
            continue
        return host_from_destination(arg)
    if index < len(argv):
        return host_from_destination(argv[index])
    return ""


def destination_of_command(command: str) -> str:
    """Destination when ``command`` itself is an ssh invocation."""

    return destination_from_argv(split_command(command))


def find_ssh_client(
    pane_pid: str,
    cmdline_map: Dict[str, str],
    ppid_map: Dict[str, str],
) -> Tuple[str, bool]:
    """``(destination, stale)`` for the deepest live ssh under the pane.

    A destination is evidence. A dead or unparsable ssh process is
    stale and does not name a host. No ssh client is ``("", False)``.
    """

    children: Dict[str, List[str]] = {}
    for pid, parent in ppid_map.items():
        children.setdefault(parent, []).append(pid)

    best_depth = -1
    best_dest = ""
    saw_stale = False
    stack = [(str(pane_pid or ""), 0)]
    seen = set()
    while stack:
        pid, depth = stack.pop()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        args = cmdline_map.get(pid, "")
        argv = split_command(args)
        if argv and program_name(argv[0]) == "ssh":
            if "<defunct>" in args.lower():
                saw_stale = True
            else:
                dest = destination_from_argv(argv)
                if dest and depth >= best_depth:
                    best_depth = depth
                    best_dest = dest
                elif not dest:
                    saw_stale = True
        for child in children.get(pid, []):
            stack.append((child, depth + 1))
    if best_dest:
        return best_dest, False
    return "", saw_stale


def ssh_connection_for_pane(
    pane_pid: str,
    expected_destination: str,
    *,
    ppid_map: Optional[Dict[str, str]] = None,
    proc_root: str = "/proc",
) -> Optional[Dict[str, str]]:
    """Return the one direct TCP SSH connection owned by this pane.

    A host name alone is not a binding. The exact ssh process must be a
    descendant of the pane shell and own one established socket. Control
    sockets, proxy commands, multiple SSH children, and unreadable process
    state fail closed.
    """

    root = str(pane_pid or "")
    expected = host_from_destination(expected_destination).casefold()
    if not root.isdigit() or not expected:
        return None
    if ppid_map is None:
        from . import process as process_detection

        ppid_map = process_detection.ppid_by_pid()

    children: Dict[str, List[str]] = {}
    for pid, parent in ppid_map.items():
        children.setdefault(str(parent), []).append(str(pid))
    stack = [root]
    seen = set()
    ssh_pids = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = Path(proc_root) / pid
        try:
            argv = [part.decode("utf-8", "replace") for part in (process / "cmdline").read_bytes().split(b"\0") if part]
            executable = os.readlink(process / "exe")
        except OSError:
            argv, executable = [], ""
        if Path(executable).name == "ssh" and destination_from_argv(argv).casefold() == expected:
            ssh_pids.append(pid)
        stack.extend(children.get(pid, ()))

    if len(ssh_pids) != 1:
        return None
    sockets = _ssh_tcp_sockets(ssh_pids[0], proc_root)
    if len(sockets) != 1:
        return None
    client_ip, client_port, server_ip, server_port = sockets[0]
    translated = _wsl_nat_source(client_ip, client_port)
    remote_client_ip, remote_client_port = translated or (client_ip, client_port)
    return {
        "ssh_client_pid": ssh_pids[0],
        "client_ip": remote_client_ip,
        "client_port": str(remote_client_port),
        "server_ip": server_ip,
        "server_port": str(server_port),
        "local_socket": (client_ip, str(client_port), server_ip, str(server_port)),
        "connection_mode": "wsl_nat" if translated else "direct",
    }


def _ssh_tcp_sockets(pid: str, proc_root: str) -> List[Tuple[str, int, str, int]]:
    process = Path(proc_root) / pid
    try:
        inodes = {
            target[8:-1]
            for fd in (process / "fd").iterdir()
            if (target := os.readlink(fd)).startswith("socket:[") and target.endswith("]")
        }
    except OSError:
        return []
    found = set()
    for table, ipv6 in (("tcp", False), ("tcp6", True)):
        try:
            lines = (process / "net" / table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01" or fields[9] not in inodes:
                continue
            local = _proc_endpoint(fields[1], ipv6)
            remote = _proc_endpoint(fields[2], ipv6)
            if local and remote and local[1] and remote[1]:
                found.add((*local, *remote))
    return sorted(found)


def _proc_endpoint(value: str, ipv6: bool) -> Optional[Tuple[str, int]]:
    try:
        address_hex, port_hex = value.split(":", 1)
        raw = bytes.fromhex(address_hex)
        if ipv6:
            if len(raw) != 16:
                return None
            raw = b"".join(raw[index:index + 4][::-1] for index in range(0, 16, 4))
        else:
            if len(raw) != 4:
                return None
            raw = raw[::-1]
        address = ipaddress.ip_address(raw)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address), int(port_hex, 16)
    except (ValueError, TypeError):
        return None


def _wsl_nat_source(client_ip: str, client_port: int) -> Optional[Tuple[str, int]]:
    """Translate one WSL socket through its unique Windows WinNAT entry."""

    try:
        if "microsoft" not in os.uname().release.lower():
            return None
        powershell = shutil.which("powershell.exe")
        if not powershell:
            return None
        address = str(ipaddress.ip_address(client_ip))
        command = (
            "$ErrorActionPreference='Stop'; "
            f"$rows = @(Get-NetNatSession | Where-Object {{ $_.InternalSourceAddress -eq '{address}' "
            f"-and $_.InternalSourcePort -eq {int(client_port)} }}); "
            "$rows | Select-Object InternalSourceAddress,InternalSourcePort,ExternalSourceAddress,ExternalSourcePort "
            "| ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        rows = json.loads(result.stdout.lstrip("\ufeff"))
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or len(rows) != 1:
            return None
        row = rows[0]
        if (
            row.get("InternalSourceAddress") != address
            or int(row.get("InternalSourcePort")) != int(client_port)
        ):
            return None
        external_ip = str(ipaddress.ip_address(row.get("ExternalSourceAddress")))
        external_port = int(row.get("ExternalSourcePort"))
        return (external_ip, external_port) if 1 <= external_port <= 65535 else None
    except (OSError, TypeError, ValueError, subprocess.TimeoutExpired):
        return None
