"""Where a Tower copy goes.

The destination follows the client that is operating Tower, not the
agent's execution host. The saved default is ``auto``. A missing config
line is ``auto``. One copy writes one primary destination.

Phone copy does not use this module. The browser keeps its own clipboard.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from .access_context import AccessContext, at_tower, context_for_process, local_access_context
from . import i18n
from .clipboard import client_rows, copy_text, native_clipboard_provider, resolve_terminal_target
from .clipboard_bridge import bridge_id_for_process, load_bridge_client
from .i18n import t

AUTO = "auto"
LOCAL_HOST = "local_host"
CURRENT_TERMINAL = "current_terminal"
TMUX_BUFFER = "tmux_buffer"
CHOICES = (AUTO, LOCAL_HOST, CURRENT_TERMINAL, TMUX_BUFFER)
MANUAL = (LOCAL_HOST, CURRENT_TERMINAL, TMUX_BUFFER)
COPY_TEST = "TOWER_COPY_TEST"

_LINE_RE = re.compile(
    r'^\s*clipboard_destination\s*=\s*"?(auto|local_host|current_terminal|tmux_buffer)"?\s*$'
)


@dataclass(frozen=True)
class CopyClient:
    """The one client Tower may treat as the operator, if that is safe."""

    tty: Optional[str]
    pid: Optional[int]
    ssh: bool
    ambiguous: bool
    bridge_id: Optional[str] = None
    access_context: Optional[AccessContext] = None


@dataclass(frozen=True)
class SessionChoice:
    """A pick that lasts only while the same clients are still attached."""

    clients: Tuple[Tuple[str, int], ...]
    destination: str


@dataclass(frozen=True)
class CopyPlan:
    destination: str
    ask: bool
    source: str


def load_preference(path: Optional[Path] = None) -> str:
    """Saved destination, or ``auto`` when the line is missing or unusable."""

    config = path or i18n.CONFIG_FILE
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            match = _LINE_RE.match(line)
            if match:
                return match.group(1)
    except Exception:
        pass
    return AUTO


def preference_configured(path: Optional[Path] = None) -> bool:
    """True only after a usable destination line has been written."""

    config = path or i18n.CONFIG_FILE
    try:
        return any(_LINE_RE.match(line) for line in config.read_text(encoding="utf-8").splitlines())
    except Exception:
        return False


def save_preference(destination: str, path: Optional[Path] = None) -> None:
    """Replace the destination line and leave every other config line alone."""

    if destination not in CHOICES:
        return
    config = path or i18n.CONFIG_FILE
    new_line = f'clipboard_destination = "{destination}"'
    try:
        existing = config.read_text(encoding="utf-8").splitlines()
    except Exception:
        existing = []
    output = []
    replaced = False
    for line in existing:
        if not replaced and _LINE_RE.match(line):
            output.append(new_line)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(new_line)
    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("\n".join(output) + "\n", encoding="utf-8")
    except Exception:
        pass


def should_offer_copy_setup(first_run: bool, configured: bool) -> bool:
    """Ask on the same first launch as language. Existing installs stay on auto."""

    return bool(first_run) and not configured


def observe(
    lines: Optional[Sequence[str]] = None,
    ssh_check: Optional[Callable[[int], bool]] = None,
) -> Tuple[CopyClient, Tuple[Tuple[str, int], ...]]:
    """Read the attached clients once."""

    if lines is None:
        from .clipboard import _client_lines

        lines = _client_lines()
    rows = client_rows(list(lines))
    target = resolve_terminal_target(list(lines), ssh_check)
    clients = tuple(sorted((tty, pid) for tty, pid, _flags in rows))
    pid = None
    if target.tty and not target.ambiguous:
        for tty, row_pid, _flags in rows:
            if tty == target.tty:
                pid = row_pid
                break
    bridge_id = None
    context = context_for_process(pid) if pid else None
    if pid:
        candidate = bridge_id_for_process(pid)
        bridge = load_bridge_client(candidate) if candidate else None
        if bridge:
            bridge_id = candidate
            context = bridge.access_context or context
    if context:
        context = at_tower(context, socket.gethostname(), liveness=context.liveness)
    if context is None and target.tty and pid and not target.ssh and len(rows) == 1:
        capability = "host" if native_clipboard_provider() else "none"
        context = local_access_context(
            target.tty, pid, tower_host=socket.gethostname(), clipboard_capability=capability
        )
    ssh = context.transport != "local" if context else bool(target.ssh)
    return CopyClient(target.tty, pid, ssh, bool(target.ambiguous), bridge_id, context), clients


def override_applies(choice: Optional[SessionChoice], clients: Tuple[Tuple[str, int], ...]) -> bool:
    """A choice dies when any attached client leaves or a new one appears."""

    if choice is None or not choice.clients or choice.destination not in MANUAL:
        return False
    return choice.clients == clients


def resolve_plan(
    preference: str,
    client: CopyClient,
    clients: Tuple[Tuple[str, int], ...],
    override: Optional[SessionChoice] = None,
) -> CopyPlan:
    """Pick one destination. An unclear client is a question, not a guess."""

    if override_applies(override, clients):
        assert override is not None
        return CopyPlan(override.destination, False, "session")
    saved = preference if preference in CHOICES else AUTO
    if saved != AUTO:
        return CopyPlan(saved, False, "saved")
    context = client.access_context
    if context and context.liveness == "live":
        if context.transport == "local":
            return (
                CopyPlan(LOCAL_HOST, False, "auto")
                if context.clipboard_capability == "host"
                else CopyPlan(AUTO, True, "auto")
            )
        if context.clipboard_capability == "bridge" and client.bridge_id:
            return CopyPlan(CURRENT_TERMINAL, False, "auto")
        return CopyPlan(AUTO, True, "auto")
    if client.ambiguous or not client.tty:
        return CopyPlan(AUTO, True, "auto")
    if client.ssh:
        return CopyPlan(AUTO, True, "auto")
    return CopyPlan(LOCAL_HOST, False, "auto")


def route_for_access_client(destination: str, client: CopyClient) -> str:
    """Use the registered remote clipboard bridge for this access client."""
    context = client.access_context
    if (
        destination == LOCAL_HOST
        and context
        and context.transport != "local"
        and context.clipboard_capability == "bridge"
        and client.bridge_id
    ):
        return CURRENT_TERMINAL
    return destination


def run_copy_test(
    preference: str,
    *,
    host_label: str = "",
    manual_destination: Optional[str] = None,
    manual_clients: Optional[Tuple[Tuple[str, int], ...]] = None,
) -> str:
    """Send the fixed test sentence to the one destination this preference names."""

    client, clients = observe()
    if manual_destination is not None and manual_clients != clients:
        return t("copy.choose_destination")
    plan = resolve_plan(preference, client, clients, None)
    if manual_destination is not None:
        allowed = (LOCAL_HOST, TMUX_BUFFER) if client.ambiguous or not client.tty else MANUAL
        if manual_destination not in allowed:
            return t("copy.choose_destination")
        plan = CopyPlan(manual_destination, False, "manual")
    elif plan.ask or (client.ambiguous and plan.destination == CURRENT_TERMINAL):
        return t("copy.choose_destination")
    if plan.destination not in MANUAL:
        return t("control.copy_failed")
    destination = route_for_access_client(plan.destination, client)
    bridge = load_bridge_client(client.bridge_id) if client.bridge_id else None
    if client.bridge_id and bridge is None:
        return t("control.copy_failed")
    outcome = copy_text(
        COPY_TEST,
        destination=destination,
        client=None if client.ambiguous else client.tty,
        ambiguous=client.ambiguous,
        ssh=client.ssh,
        bridge_port=bridge.port if bridge else None,
        bridge_token=bridge.token if bridge else None,
    )
    return copy_test_notice(outcome, host_label)


def copy_test_notice(outcome, host_label: str = "") -> str:
    """Say the test was sent. Do not claim a person has pasted it."""

    del host_label
    if outcome.buffer:
        return t("control.copied_buffer")
    if outcome.clipboard or outcome.terminal_requested:
        return t("copy.test_sent")
    return t("control.copy_failed")
