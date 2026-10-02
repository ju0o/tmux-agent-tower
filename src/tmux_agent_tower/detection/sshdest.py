"""Find an ssh client in a pane's process tree and read its destination.

The destination is an argv token, not a word inside a shell string.
``bash -c 'echo ssh asus'`` is not an ssh client. A wrapper is only
an ssh client when a process whose program is ``ssh`` is actually in
the tree.
"""

from __future__ import annotations

import shlex
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
