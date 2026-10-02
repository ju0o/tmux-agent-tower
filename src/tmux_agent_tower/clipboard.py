"""Copy text to the system clipboard, with a tmux buffer fallback.

The result body is written to the tool's stdin. It is never placed in
argv, and nothing here uses ``shell=True``. A missing tool is not
reported as a successful copy.
"""

from __future__ import annotations

import os
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


def copy_text(
    text: str,
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    run: Optional[Callable[[List[str], bytes], int]] = None,
    environ: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
) -> CopyOutcome:
    """Copy ``text``. System clipboard first, then ``tmux load-buffer``."""

    finder = which or shutil.which
    runner = run or _default_run
    env = environ if environ is not None else _environ()
    platform = system or sys.platform
    tool = choose_clipboard_tool(finder, env, platform)
    if tool:
        path = finder(tool) or tool
        if runner(_argv(tool, path), _payload(tool, text)) == 0:
            return CopyOutcome(True, False, tool)
    if runner(["tmux", "load-buffer", "-"], text.encode("utf-8")) == 0:
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
