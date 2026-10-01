"""Opt-in smart ``Ctrl+b w``.

The binding itself never changes when Tower starts or exits. One tmux
command checks the session registration at key-press time:

* a live registered Tower pane -> ``tower --focus``
* anything else -> the ``w`` command recorded at install time

Public installs do not write this on their own. ``tower keys install``
(or the in-TUI settings screen) is the only opt-in. That command records
the current ``w`` binding first. A binding Tower itself already owns is
not treated as the user's command; the fallback is then the ``w``
command from a tmux server started with an empty config, queried on
this machine, never a guessed string.

``~/.tmux.conf`` is not rewritten. Only the marked block is inserted,
replaced, or removed, and the file is copied aside before the first
change.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import capture

BEGIN = "# >>> tmux-agent-tower smart-w >>>"
END = "# <<< tmux-agent-tower smart-w <<<"
OLD_BLOCKS = (
    (
        "# >>> tmux-agent-tower v0.1.1 focus binding >>>",
        "# <<< tmux-agent-tower v0.1.1 focus binding <<<",
    ),
)
STATE_NAME = "smart-w.json"
CHECK_SHELL = "tower --has-active"
FOCUS_SHELL = 'run-shell -b "tower --focus"'
_OWNED_MARKERS = ("tower --focus", "tower --has-active", "tmux-agent-tower")


def config_path() -> Path:
    return Path.home() / ".tmux.conf"


def state_path() -> Path:
    return Path.home() / ".config" / "tmux-agent-tower" / STATE_NAME


def parse_w_command(list_keys_line: str) -> str:
    """The command half of one ``list-keys -T prefix w`` line.

    ``bind-key -T prefix w choose-tree -Zw`` -> ``choose-tree -Zw``.
    Empty when the line is not a prefix ``w`` binding.
    """

    text = (list_keys_line or "").strip()
    marker = " w "
    if "prefix" not in text or marker not in text:
        return ""
    return text.split(marker, 1)[1].strip()


def is_tower_owned(command: str) -> bool:
    return any(marker in (command or "") for marker in _OWNED_MARKERS)


def choose_fallback(list_keys_line: str, tmux_default: str, saved: str = "") -> tuple[str, str]:
    """``(command, source)``. A saved command always wins so a second
    install cannot overwrite the user's original with our own binding.
    """

    if saved.strip():
        return saved.strip(), "saved"
    current = parse_w_command(list_keys_line)
    if current and not is_tower_owned(current):
        return current, "current"
    if not tmux_default.strip():
        return "", "missing"
    return tmux_default.strip(), "tmux-default"


def tower_invocation() -> str:
    """The ``tower`` that is installing the binding, not whatever ``PATH``
    happens to find. An older install on ``PATH`` would make ``--has-active``
    fail and the key would never focus.
    """

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        exe = Path(argv0)
        if exe.is_file() and "tower" in exe.name:
            return str(exe.resolve())
    return "tower"


def render_block(fallback: str, tower_cmd: str = "tower") -> str:
    quoted = "'" + fallback.replace("'", "'\\''") + "'"
    check = f"{tower_cmd} --has-active"
    focus = f'run-shell -b "{tower_cmd} --focus"'
    return "\n".join([
        BEGIN,
        "# Ctrl+b w: registered Tower pane when one is alive, otherwise the",
        "# w command recorded when this block was installed.",
        "unbind-key -T prefix w",
        f"bind-key -T prefix w if-shell '{check}' '{focus}' {quoted}",
        END,
        "",
    ])


def strip_managed(text: str) -> str:
    cleaned = text or ""
    pairs = ((BEGIN, END),) + OLD_BLOCKS
    for begin, end in pairs:
        while True:
            start = cleaned.find(begin)
            if start < 0:
                break
            stop = cleaned.find(end, start)
            if stop < 0:
                cleaned = cleaned[:start]
                break
            stop += len(end)
            if stop < len(cleaned) and cleaned[stop] == "\n":
                stop += 1
            cleaned = cleaned[:start] + cleaned[stop:]
    return cleaned


def upsert_block(text: str, fallback: str, tower_cmd: str = "tower") -> str:
    cleaned = strip_managed(text).rstrip()
    block = render_block(fallback, tower_cmd).rstrip()
    if cleaned:
        return cleaned + "\n\n" + block + "\n"
    return block + "\n"


def block_installed(text: str) -> bool:
    return BEGIN in (text or "") and END in (text or "")


def load_saved_fallback(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    fallback = data.get("fallback")
    return fallback.strip() if isinstance(fallback, str) else ""


def binding_args(fallback: str, tower_cmd: str = "tower") -> list[str]:
    return [
        "bind-key", "-T", "prefix", "w",
        "if-shell", f"{tower_cmd} --has-active",
        f'run-shell -b "{tower_cmd} --focus"',
        fallback,
    ]


def restore_args(fallback: str) -> list[str]:
    parts = shlex.split(fallback)
    return ["bind-key", "-T", "prefix", "w", *parts]


def _backup_once(path: Path) -> None:
    if not path.exists():
        return
    backup = path.with_name(path.name + ".tower-backup")
    if backup.exists():
        return
    shutil.copy2(path, backup)


def install_smart_w(
    conf_path: Path,
    state_file: Path,
    *,
    list_keys_line: str,
    tmux_default: str,
    apply: Optional[Callable[[list[str]], None]] = None,
    tower_cmd: str = "tower",
) -> str:
    """Write the managed block and return the fallback command.

    Raises ``RuntimeError`` when no fallback can be determined. Does not
    touch the files in that case.
    """

    saved = load_saved_fallback(state_file)
    fallback, source = choose_fallback(list_keys_line, tmux_default, saved)
    if not fallback:
        raise RuntimeError("no fallback")

    previous = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    updated = upsert_block(previous, fallback, tower_cmd)
    if updated != previous:
        _backup_once(conf_path)
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(updated, encoding="utf-8")

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"fallback": fallback, "source": source}, indent=2) + "\n",
        encoding="utf-8",
    )
    if apply is not None:
        apply(binding_args(fallback, tower_cmd))
    return fallback


def restore_smart_w(
    conf_path: Path,
    state_file: Path,
    *,
    apply: Optional[Callable[[list[str]], None]] = None,
) -> str:
    """Remove only the managed block and re-bind ``w`` to the saved command."""

    fallback = load_saved_fallback(state_file)
    previous = conf_path.read_text(encoding="utf-8") if conf_path.exists() else ""
    updated = strip_managed(previous).rstrip()
    if updated:
        updated += "\n"
    if updated != previous and conf_path.exists():
        _backup_once(conf_path)
        conf_path.write_text(updated, encoding="utf-8")
    if apply is not None and fallback:
        apply(restore_args(fallback))
    return fallback


def query_w_command(argv_prefix: list[str]) -> str:
    try:
        proc = subprocess.run(
            [*argv_prefix, "list-keys", "-T", "prefix", "w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:
        return ""
    return parse_w_command(proc.stdout)


def query_tmux_default_w() -> str:
    """``w`` from a tmux server that loaded no config file.

    ``-f /dev/null`` has to be on the same invocation that creates the
    server. A later ``list-keys`` without it can start a normal server
    and report the user's own binding back as if it were the default.
    A Tower-owned command is never a default.
    """

    sock = f"tower-default-w-{os.getpid()}"
    base = ["tmux", "-f", "/dev/null", "-L", sock]
    try:
        command = query_w_command(base)
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=False)
    if is_tower_owned(command):
        return ""
    return command


def install_for_user() -> str:
    default = query_tmux_default_w()
    current = query_w_command(["tmux"])
    fallback = install_smart_w(
        config_path(),
        state_path(),
        list_keys_line=current,
        tmux_default=default,
        apply=lambda args: capture.run_tmux(args, capture=False),
        tower_cmd=tower_invocation(),
    )
    return fallback


def restore_for_user() -> str:
    return restore_smart_w(
        config_path(),
        state_path(),
        apply=lambda args: capture.run_tmux(args, capture=False),
    )


def installed_for_user() -> bool:
    try:
        return block_installed(config_path().read_text(encoding="utf-8"))
    except Exception:
        return False
