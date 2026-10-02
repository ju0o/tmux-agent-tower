"""Copy text to the system clipboard, with a tmux buffer fallback.

The result body is written to the tool's stdin. It is never placed in
argv, and nothing here uses ``shell=True``. A missing tool is not
reported as a successful copy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class CopyOutcome:
    """``clipboard`` is a real system clipboard. ``buffer`` is tmux only."""

    clipboard: bool
    buffer: bool
    tool: str


OSC_BYTES = 48 * 1024
_CLIENT_TTY = re.compile(r"^/dev/pts/\d+$")
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _default_run(argv: List[str], data: bytes) -> int:
    try:
        proc = subprocess.run(
            argv,
            input=data,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return 1
    return proc.returncode


def choose_clipboard_tool(
    which: Callable[[str], Optional[str]],
    environ: Dict[str, str],
    system: str,
) -> str:
    """First installed tool for this machine. Empty when none exist."""

    release = environ.get("TOWER_UNAME", "")
    wsl = system == "win32" or "microsoft" in release.lower()
    if wsl and which("clip.exe"):
        return "clip.exe"
    if system == "darwin" and which("pbcopy"):
        return "pbcopy"
    if environ.get("WAYLAND_DISPLAY") and which("wl-copy"):
        return "wl-copy"
    if which("xclip"):
        return "xclip"
    if which("xsel"):
        return "xsel"
    return ""


def _argv(tool: str, path: str) -> List[str]:
    if tool == "xclip":
        return [path, "-selection", "clipboard"]
    if tool == "xsel":
        return [path, "--clipboard", "--input"]
    return [path]


def _payload(tool: str, text: str) -> bytes:
    # clip.exe reads UTF-16LE from a pipe. UTF-8 Korean becomes mojibake.
    if tool == "clip.exe":
        return text.encode("utf-16le")
    return text.encode("utf-8")


def sole_client_tty(lines: List[str]) -> Optional[str]:
    """The one attached tty, or None when zero or several clients exist.

    Several attached clients do not get a guessed clipboard.
    """

    ttys = [line.strip() for line in lines if line.strip()]
    if len(ttys) != 1 or not _CLIENT_TTY.match(ttys[0]):
        return None
    return ttys[0]


def terminal_clipboard_client(
    lines: Optional[List[str]] = None,
    ssh_check: Optional[Callable[[int], bool]] = None,
) -> Optional[str]:
    """TTY for OSC 52 only when one client is attached and it came in over SSH.

    A local client keeps the host clipboard (``clip.exe`` on MainPC).
    Two clients are not a guess about who pressed Y.
    """

    if lines is None:
        from .tmux.capture import run_tmux

        lines = run_tmux(["list-clients", "-F", "#{client_tty}\t#{client_pid}"]).splitlines()
    rows = []
    for line in lines:
        parts = line.strip().split("\t")
        if len(parts) != 2 or not parts[1].isdigit() or not _CLIENT_TTY.match(parts[0]):
            continue
        rows.append((parts[0], int(parts[1])))
    if len(rows) != 1:
        return None
    tty, pid = rows[0]
    check = ssh_check or _pid_came_through_ssh
    if not check(pid):
        return None
    return tty


def _pid_came_through_ssh(pid: int) -> bool:
    """True only with SSH evidence on this process or an ancestor."""

    seen = set()
    current = pid
    while current and current not in seen and current > 1:
        seen.add(current)
        if _process_has_ssh_evidence(current):
            return True
        current = _parent_pid(current)
    return False


def _process_has_ssh_evidence(pid: int) -> bool:
    environ = _read_proc(f"/proc/{pid}/environ")
    if b"SSH_CONNECTION=" in environ or b"SSH_CLIENT=" in environ:
        return True
    comm = _read_proc(f"/proc/{pid}/comm").decode("utf-8", "replace").strip()
    return comm == "sshd"


def _parent_pid(pid: int) -> int:
    raw = _read_proc(f"/proc/{pid}/stat").decode("utf-8", "replace")
    end = raw.rfind(")")
    if end < 0:
        return 0
    fields = raw[end + 2 :].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return 0
    return int(fields[1])


def _read_proc(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except Exception:
        return b""


def plain_clipboard_text(text: str) -> str:
    """Drop ANSI sequences and other control bytes. Newlines and tabs stay."""

    cleaned = _ANSI_RE.sub("", text or "")
    kept = []
    for char in cleaned:
        if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127):
            kept.append(char)
    return "".join(kept)


def copy_text(
    text: str,
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    run: Optional[Callable[[List[str], bytes], int]] = None,
    environ: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
    client: Optional[str] = None,
) -> CopyOutcome:
    """Copy ``text``. One terminal client, then the host clipboard, then tmux."""

    finder = which or shutil.which
    runner = run or _default_run
    env = environ if environ is not None else _environ()
    platform = system or sys.platform
    body = plain_clipboard_text(text)
    data = body.encode("utf-8")
    if client and len(data) <= OSC_BYTES:
        code = runner(["tmux", "load-buffer", "-w", "-t", client, "-"], data)
        if code == 0:
            return CopyOutcome(True, False, "terminal")
    tool = choose_clipboard_tool(finder, env, platform)
    if tool:
        path = finder(tool) or tool
        if runner(_argv(tool, path), _payload(tool, body)) == 0:
            return CopyOutcome(True, False, tool)
    if runner(["tmux", "load-buffer", "-"], data) == 0:
        return CopyOutcome(False, True, "tmux")
    return CopyOutcome(False, False, tool or "none")


def _environ() -> Dict[str, str]:
    release = ""
    try:
        release = os.uname().release
    except Exception:
        release = ""
    env = dict(os.environ)
    env["TOWER_UNAME"] = release
    return env
