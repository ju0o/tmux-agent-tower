"""Opt-in smart ``Ctrl+b w``.

The binding itself never changes when Tower starts or exits. One tmux
command checks the session registration at key-press time:

* a live registered Tower pane in this session -> ``tower --focus``
* anything else -> the user's ``w`` command

Public installs do not write this on their own. ``tower keys install``
(or the in-TUI settings screen) is the only opt-in. That command reads
``list-keys -T prefix`` first. A binding Tower itself already owns is
not the user's command. The next place to look is the user's config
with the Tower block removed (so a ``source-file`` that binds ``w`` is
kept). Only when that still has no ``w`` command do we query a tmux
server started with an empty config. The default is never a guessed
string.

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
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

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


def parse_w_command(list_keys_output: str) -> str:
    """The command half of a ``list-keys -T prefix w`` result.

    ``bind-key -T prefix w choose-tree -Zw`` -> ``choose-tree -Zw``.
    Empty when no line is a prefix ``w`` binding. A bare command such as
    ``choose-tree -Zw`` is not a list-keys line and is not accepted.
    """

    for line in (list_keys_output or "").splitlines():
        text = line.strip()
        marker = " w "
        if "prefix" not in text or marker not in text:
            continue
        return text.split(marker, 1)[1].strip()
    return ""


def is_tower_owned(command: str) -> bool:
    return any(marker in (command or "") for marker in _OWNED_MARKERS)


def choose_fallback(
    list_keys_line: str,
    tmux_default: str,
    saved: str = "",
    saved_source: str = "",
    config_line: str = "",
) -> Tuple[str, str]:
    """``(command, source)`` for the ``w`` action used when Tower is absent.

    A live user binding wins. When the live key is one Tower installed,
    the command from the user's config (Tower block removed) wins over a
    previously saved empty-server default. A saved user command is the
    safety net when that config cannot be read. The empty-server command
    is last, and only if it was actually queried.
    """

    live = parse_w_command(list_keys_line)
    if live and not is_tower_owned(live):
        return live, "current"

    configured = parse_w_command(config_line)
    if configured and not is_tower_owned(configured):
        return configured, "config"

    if saved.strip() and not is_tower_owned(saved) and saved_source != "tmux-default":
        return saved.strip(), "saved"

    if not tmux_default.strip() or is_tower_owned(tmux_default):
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


def load_saved_record(path: Path) -> Tuple[str, str]:
    """``(fallback command, source)``. Missing source is ``saved`` so an
    older state file still protects a command the user already recorded.
    """

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    fallback = data.get("fallback")
    command = fallback.strip() if isinstance(fallback, str) else ""
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        source = "saved" if command else ""
    return command, source.strip()


def load_saved_fallback(path: Path) -> str:
    command, _source = load_saved_record(path)
    return command


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
    config_line: str = "",
) -> str:
    """Write the managed block and return the fallback command.

    Raises ``RuntimeError`` when no fallback can be determined. Does not
    touch the files in that case.
    """

    saved, saved_source = load_saved_record(state_file)
    fallback, source = choose_fallback(
        list_keys_line, tmux_default, saved, saved_source, config_line
    )
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


def query_w_line(argv_prefix: list[str]) -> str:
    """Raw ``list-keys -T prefix w`` output. Callers parse it themselves
    so a command is not stripped and then rejected as "not a list-keys line".
    """

    try:
        proc = subprocess.run(
            [*argv_prefix, "list-keys", "-T", "prefix", "w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:
        return ""
    return proc.stdout or ""


def query_w_command(argv_prefix: list[str]) -> str:
    return parse_w_command(query_w_line(argv_prefix))


def query_configured_w(conf_path: Path) -> str:
    """``w`` as the user's config would bind it without the Tower block.

    Uses a private tmux socket so the running server is not touched.
    A ``source-file`` in that config still applies, which is how a custom
    ``w`` survives when the live key is already Tower's.
    """

    try:
        text = conf_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    stripped = strip_managed(text)
    handle = tempfile.NamedTemporaryFile("w", prefix="tower-w-", suffix=".conf", delete=False, encoding="utf-8")
    tmp = Path(handle.name)
    try:
        handle.write(stripped)
        handle.close()
        sock = f"tower-config-w-{os.getpid()}"
        base = ["tmux", "-f", str(tmp), "-L", sock]
        try:
            return query_w_line(base)
        finally:
            subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=False)
    finally:
        tmp.unlink(missing_ok=True)


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
    live = query_w_line(["tmux"])
    configured = query_configured_w(config_path())
    fallback = install_smart_w(
        config_path(),
        state_path(),
        list_keys_line=live,
        tmux_default=default,
        apply=lambda args: capture.run_tmux(args, capture=False),
        tower_cmd=tower_invocation(),
        config_line=configured,
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
