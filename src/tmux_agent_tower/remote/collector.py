"""Minimal multi-host prototype (P3).

Deliberately the smallest thing that could work: no daemon, no new service,
no credentials beyond the user's existing ``ssh <alias>`` setup. The remote host
runs a *read-only* one-shot ``tmux list-panes`` / ``tmux capture-pane``
query over the user's existing SSH connection and parses the result.
Nothing is installed on the remote host, and nothing this module does can
kill, restart, or send input to a remote pane or process.

Failure handling is graceful by design: any SSH error, timeout, or
malformed output degrades that single host to ``status: "OFFLINE"`` (or
``"UNKNOWN"`` for malformed data) with an empty pane list -- it never
raises, and it never blocks the local TUI beyond ``timeout`` seconds.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import subprocess
from typing import Dict, List

HOST_STATUS_ONLINE = "ONLINE"
HOST_STATUS_OFFLINE = "OFFLINE"
HOST_STATUS_UNKNOWN = "UNKNOWN"

DEFAULT_TIMEOUT = 4.0
_PANE_ID_RE = re.compile(r"^%\d+$")
_PID_RE = re.compile(r"^\d+$")
_SESSION_ID_RE = re.compile(r"^\$\d+$")
_GROK_SESSION_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_GROK_RESULT_MARKER = "__TOWER_GROK_RESULT__"
_MAX_GROK_RESULT_BYTES = 2 * 1024 * 1024


# A single remote invocation: list every pane's identifying fields, then for
# each pane id capture its tail. Kept as one SSH round trip for latency.
_REMOTE_SNAPSHOT_SCRIPT = r"""
set -u
if ! command -v tmux >/dev/null 2>&1; then
    echo "__TOWER_NO_TMUX__"
    exit 0
fi
if ! tmux list-sessions >/dev/null 2>&1; then
    echo "__TOWER_NO_SERVER__"
    exit 0
fi
SEP=$(printf '\037')
tmux list-panes -a -F "#{session_name}${SEP}#{session_id}${SEP}#{window_id}${SEP}#{window_index}${SEP}#{window_name}${SEP}#{window_created}${SEP}#{pane_index}${SEP}#{pane_id}${SEP}#{pane_title}${SEP}#{pane_current_command}${SEP}#{pane_current_path}${SEP}#{pane_pid}${SEP}#{pane_dead}"
"""


_REMOTE_GROK_SCRIPT = r'''import hashlib, ipaddress, json, os, re, subprocess, sys
MARKER = "__TOWER_GROK_RESULT__"
PROC = sys.argv[5] if len(sys.argv) > 5 else "/proc"
SID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
HEADING_RE = re.compile(r"(?m)^## (User|Assistant)\s*$")
MAX_EXPORT = 2 * 1024 * 1024

def emit(status, **fields):
    print(MARKER + json.dumps({"status": status, **fields}, ensure_ascii=False, separators=(",", ":")))

def norm_ip(value):
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return ""

def proc_ip(value, ipv6):
    try:
        raw = bytes.fromhex(value)
        if ipv6:
            if len(raw) != 16:
                return ""
            raw = b"".join(raw[index:index + 4][::-1] for index in range(0, 16, 4))
        else:
            if len(raw) != 4:
                return ""
            raw = raw[::-1]
        return norm_ip(str(ipaddress.ip_address(raw)))
    except ValueError:
        return ""

def connection_live(expected):
    for table, ipv6 in (("tcp", False), ("tcp6", True)):
        try:
            lines = open(os.path.join(PROC, "net", table), encoding="ascii").read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "01":
                continue
            try:
                local_ip, local_port = fields[1].split(":", 1)
                peer_ip, peer_port = fields[2].split(":", 1)
                actual = (proc_ip(peer_ip, ipv6), str(int(peer_port, 16)),
                          proc_ip(local_ip, ipv6), str(int(local_port, 16)))
            except ValueError:
                continue
            if actual == expected:
                return True
    return False

def process(pid):
    base = os.path.join(PROC, pid)
    try:
        raw = open(os.path.join(base, "stat"), "rb").read().decode("ascii", "replace")
        end = raw.rfind(")")
        fields = raw[end + 2:].split()
        comm = open(os.path.join(base, "comm"), encoding="ascii", errors="replace").read().strip()
        return {"ppid": fields[1], "comm": comm}
    except (OSError, IndexError):
        return None

def ssh_connection(pid):
    try:
        raw = open(os.path.join(PROC, pid, "environ"), "rb").read().split(b"\0")
        env = dict(part.split(b"=", 1) for part in raw if b"=" in part)
        fields = env.get(b"SSH_CONNECTION", b"").decode("ascii", "strict").split()
        if len(fields) != 4:
            return ()
        return (norm_ip(fields[0]), str(int(fields[1])), norm_ip(fields[2]), str(int(fields[3])))
    except (OSError, ValueError, UnicodeError):
        return ()

def descendants(root, parents):
    children = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    result, stack = set(), [root]
    # ponytail: cap /proc ancestry at 4096 processes; raise it if large hosts need coverage.
    while stack and len(result) < 4096:
        pid = stack.pop()
        if pid in result:
            continue
        result.add(pid)
        stack.extend(children.get(pid, ()))
    return result

def grok_sessions(pids, expected):
    found = {}
    for pid in pids:
        base = os.path.join(PROC, pid)
        if process(pid) is None or process(pid)["comm"].lower() != "grok" or ssh_connection(pid) != expected:
            continue
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                path = os.readlink(os.path.join(base, "fd", fd)).removesuffix(" (deleted)")
                if not path.endswith("/events.jsonl"):
                    continue
                sid = os.path.basename(os.path.dirname(path))
                exe = os.readlink(os.path.join(base, "exe")).removesuffix(" (deleted)")
                info = os.stat(path)
                if SID_RE.fullmatch(sid):
                    found[(pid, sid, path, exe, info.st_mtime_ns, info.st_size)] = None
            except OSError:
                continue
    return list(found)

def latest_turn(path, sid):
    number, ended = None, False
    try:
        with open(path, encoding="utf-8", errors="replace") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if event.get("type") == "turn_started" and event.get("session_id") == sid:
                    value = event.get("turn_number")
                    if isinstance(value, int) and 0 <= value <= 10000:
                        number, ended = value, False
                elif event.get("type") == "turn_ended" and number is not None:
                    ended = True
    except OSError:
        return None, False
    return number, ended

def answer_from_export(raw, turn):
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeError:
        return ""
    headings = list(HEADING_RE.finditer(text))
    roles = [match.group(1) for match in headings]
    expected = [role for _ in range(turn + 1) for role in ("User", "Assistant")]
    if roles != expected or not roles or roles[-1] != "Assistant":
        return ""
    return text[headings[-1].end():].strip()

try:
    expected = (norm_ip(sys.argv[1]), str(int(sys.argv[2])), norm_ip(sys.argv[3]), str(int(sys.argv[4])))
except (IndexError, ValueError):
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
if not all(expected):
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
if not connection_live(expected):
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)

processes = {}
for name in os.listdir(PROC):
    if name.isdigit():
        item = process(name)
        if item:
            processes[name] = item
matching = {pid for pid in processes if ssh_connection(pid) == expected}
roots = [pid for pid in matching if processes[pid]["ppid"] not in matching]
if len(roots) != 1:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
root = roots[0]
sshd = ""
if processes[root]["comm"].lower().startswith("sshd"):
    sshd = root
parent = processes[root]["ppid"]
for _ in range(32 if not sshd else 0):
    item = processes.get(parent)
    if not item or parent in {"0", "1"}:
        break
    if item["comm"].lower().startswith("sshd"):
        sshd = parent
        break
    parent = item["ppid"]
if not sshd:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
sessions = grok_sessions(descendants(root, {pid: item["ppid"] for pid, item in processes.items()}), expected)
if len(sessions) != 1:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
grok_pid, sid, events_path, executable, _mtime, _size = sessions[0]
turn, turn_complete = latest_turn(events_path, sid)
binding = {"remote_session_pid": root, "remote_sshd_pid": sshd, "grok_pid": grok_pid,
           "session_id": sid, "ssh_connection": list(expected)}
if turn is None or not turn_complete:
    emit("REMOTE_RESULT_INCOMPLETE", **binding, turn_complete=False, body_complete=False)
    raise SystemExit(0)
try:
    exported = subprocess.run([executable, "export", sid], capture_output=True, timeout=6, check=False)
    raw = exported.stdout or b""
except (OSError, subprocess.TimeoutExpired):
    emit("REMOTE_PROVIDER_UNAVAILABLE", **binding, turn_complete=True, body_complete=False)
    raise SystemExit(0)
if exported.returncode != 0 or not raw or len(raw) > MAX_EXPORT:
    emit("REMOTE_PROVIDER_UNAVAILABLE", **binding, turn_complete=True, body_complete=False)
    raise SystemExit(0)
body = answer_from_export(raw, turn)
body_bytes = body.encode("utf-8")
if not body or len(body_bytes) > MAX_EXPORT:
    emit("REMOTE_RESULT_INCOMPLETE", **binding, turn_complete=True, body_complete=False)
    raise SystemExit(0)
emit("ok", **binding, turn_number=turn, turn_complete=True, body_complete=True,
     native_length=len(body), native_utf8_bytes=len(body_bytes),
     native_sha256=hashlib.sha256(body_bytes).hexdigest(), text=body)
'''

def _run_ssh(host_alias: str, script: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(timeout))}",
            host_alias,
            script,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout + 1.0,
        check=False,
    )


def fetch_remote(host_alias: str, display_name: str = None, timeout: float = DEFAULT_TIMEOUT) -> Dict:
    """Fetch a best-effort snapshot of ``host_alias``'s tmux panes.

    Read-only: only ``tmux list-panes``/``tmux capture-pane`` are ever run
    on the remote side, over the user's own pre-existing SSH alias.
    """

    display_name = display_name or host_alias

    try:
        result = _run_ssh(host_alias, _REMOTE_SNAPSHOT_SCRIPT, timeout)
    except subprocess.TimeoutExpired:
        return {"host": display_name, "status": HOST_STATUS_OFFLINE, "panes": []}
    except Exception:
        return {"host": display_name, "status": HOST_STATUS_OFFLINE, "panes": []}

    if result.returncode != 0:
        return {"host": display_name, "status": HOST_STATUS_OFFLINE, "panes": []}

    return parse_remote_snapshot(display_name, result.stdout)


def fetch_remote_history(
    host_alias: str,
    pane_id: str,
    pane_pid: str,
    lines: int = 800,
    timeout: float = DEFAULT_TIMEOUT,
    session_id: str = "",
    join_wrapped: bool = False,
) -> List[str] | None:
    """Read bounded history for one registered remote tmux pane.

    Pane PID must still match the overview snapshot, so a reused pane ID
    cannot redirect Result extraction to a different process.
    """

    if (
        not host_alias
        or host_alias.startswith("-")
        or any(ch.isspace() or not ch.isprintable() for ch in host_alias)
        or not _PANE_ID_RE.fullmatch(pane_id or "")
        or not _PID_RE.fullmatch(str(pane_pid or ""))
        or not 1 <= lines <= 1200
        or (session_id and not _SESSION_ID_RE.fullmatch(session_id))
    ):
        return None
    session_check = (
        f"test \"$(tmux display-message -p -t {pane_id} '#{{session_id}}')\" = {shlex.quote(session_id)} || exit 4; "
        if session_id else ""
    )
    script = (
        f"test \"$(tmux display-message -p -t {pane_id} '#{{pane_pid}}')\" = {pane_pid} || exit 4; "
        f"test \"$(tmux display-message -p -t {pane_id} '#{{pane_dead}}')\" = 0 || exit 4; "
        f"{session_check}"
        f"tmux capture-pane -p{' -J' if join_wrapped else ''} -t {pane_id} -S -{lines}"
    )
    try:
        result = _run_ssh(host_alias, script, timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").rstrip("\n").split("\n") if result.stdout else []


def fetch_remote_grok_result(
    host_alias: str,
    connection: Dict[str, str],
    timeout: float = 10.0,
) -> Dict | None:
    """Read one Grok result only from the SSH session matching this socket."""

    if (
        not host_alias
        or host_alias.startswith("-")
        or any(ch.isspace() or not ch.isprintable() for ch in host_alias)
        or not isinstance(connection, dict)
        or not str(connection.get("ssh_client_pid") or "").isdecimal()
    ):
        return None
    try:
        client_ip = _normalize_ip(connection["client_ip"])
        client_port = int(connection["client_port"])
        server_ip = _normalize_ip(connection["server_ip"])
        server_port = int(connection["server_port"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (client_ip and server_ip and 1 <= client_port <= 65535 and 1 <= server_port <= 65535):
        return None
    session_id = str(connection.get("remote_session_id") or "")
    if session_id and not _GROK_SESSION_RE.fullmatch(session_id):
        return None
    args = " ".join(shlex.quote(value) for value in (client_ip, str(client_port), server_ip, str(server_port)))
    script = f"python3 -c {shlex.quote(_REMOTE_GROK_SCRIPT)} {args}"
    try:
        completed = _run_ssh(host_alias, script, timeout)
    except Exception:
        return None
    if completed.returncode != 0 or len((completed.stdout or "").encode("utf-8", "replace")) > _MAX_GROK_RESULT_BYTES + 65536:
        return None
    record = next(
        (line[len(_GROK_RESULT_MARKER):] for line in reversed((completed.stdout or "").splitlines())
         if line.startswith(_GROK_RESULT_MARKER)),
        "",
    )
    try:
        payload = json.loads(record)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") not in {
        "ok", "REMOTE_PROVIDER_UNBOUND", "REMOTE_PROVIDER_UNAVAILABLE", "REMOTE_RESULT_INCOMPLETE",
    }:
        return None
    return payload


def _normalize_ip(value: str) -> str:
    address = ipaddress.ip_address(str(value).split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def parse_remote_snapshot(display_name: str, raw_stdout: str) -> Dict:
    text = (raw_stdout or "").strip()

    if not text or text == "__TOWER_NO_TMUX__":
        return {"host": display_name, "status": HOST_STATUS_UNKNOWN, "panes": []}

    if text == "__TOWER_NO_SERVER__":
        # Reachable, tmux installed, but nothing running -- a legitimate,
        # non-error "online with zero panes" state.
        return {"host": display_name, "status": HOST_STATUS_ONLINE, "panes": []}

    panes: List[Dict] = []

    for line in text.split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 12:  # older Tower peer without the window creation marker
            parts.insert(5, "")
        if len(parts) != 13:
            # Malformed line: skip it rather than failing the whole host.
            continue

        (
            session,
            session_id,
            window_id,
            window_index,
            window_name,
            window_created,
            pane_index,
            pane_id,
            title,
            command,
            path,
            pane_pid,
            dead,
        ) = parts

        panes.append(
            {
                "session": session,
                "session_id": session_id,
                "window_id": window_id,
                "window_index": window_index,
                "window_name": window_name,
                "window_created": window_created,
                "pane_index": pane_index,
                "pane_id": pane_id,
                "pane_pid": pane_pid,
                "title": title or "(unnamed)",
                "command": command,
                "cmdline": "",
                "path": path,
                "dead": dead == "1",
                # No content capture in the v0.1.0 prototype snapshot (kept
                # to a single SSH round trip); status is therefore
                # necessarily coarser than local panes -- see README limits.
                "lines": [],
            }
        )

    if not panes:
        return {"host": display_name, "status": HOST_STATUS_ONLINE, "panes": []}

    return {"host": display_name, "status": HOST_STATUS_ONLINE, "panes": panes}
