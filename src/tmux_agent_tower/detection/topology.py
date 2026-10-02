"""Where a pane is observed, where its tmux server is, and where work runs.

Write actions keep using tmux_host, session, window id, and pane id.
execution_host only decides which group the row is shown in.
"""

from __future__ import annotations

from typing import Optional

from ..hostreg import HostRegistry
from .identity import _title_agent, _ui_agent


def physical_identity(row: Dict) -> tuple:
    """The tmux object this row controls. Pane ids are not globally unique."""

    return (
        str(row.get("tmux_host") or ""),
        str(row.get("session") or ""),
        str(row.get("pane_id") or ""),
        bool(row.get("remote")),
    )


def distinct_physical(rows) -> list:
    """Drop only exact physical duplicates. Never merge across tmux servers."""

    seen = []
    out = []
    for row in rows:
        ident = physical_identity(row)
        if ident in seen:
            continue
        seen.append(ident)
        out.append(row)
    return out


def classify_transport(
    *,
    tmux_host: str,
    registry: HostRegistry,
    ssh_target: str = "",
    ssh_stale: bool = False,
    resolver=None,
    binding: Optional[Dict[str, str]] = None,
    override_host: str = "",
) -> Dict[str, str]:
    """Host fields for one local tmux pane.

    A saved execution host wins over the process tree. A launcher binding
    wins over inference. A stale ssh process does not move the pane off
    the tmux host. ``SSH_CONNECTION`` is ignored: the observer is the
    machine this process is running on, which the caller already put in
    the registry.
    """

    detected, stale = (ssh_target or ""), bool(ssh_stale)
    bound = binding or {}
    bound_transport = str(bound.get("transport") or "")
    bound_target = str(bound.get("transport_target") or "")
    bound_host = str(bound.get("execution_host") or "")

    if override_host:
        execution = override_host
        target = bound_target or detected
        transport = "ssh" if execution != tmux_host or target else "local"
        source = "override"
    elif bound_transport == "ssh" and (bound_host or bound_target):
        target = bound_target or detected
        execution = bound_host or (registry.resolve(target, resolver) if target else tmux_host)
        transport = "ssh"
        source = "binding"
    elif detected:
        target = detected
        execution = registry.resolve(detected, resolver) or tmux_host
        transport = "ssh"
        source = "process"
    elif stale:
        execution = tmux_host
        target = ""
        transport = "unknown"
        source = "stale"
    else:
        execution = tmux_host
        target = ""
        transport = "local"
        source = "local"

    return {
        "observer_host": registry.observer,
        "tmux_host": tmux_host,
        "execution_host": execution or tmux_host,
        "transport": transport,
        "transport_target": target,
        "topology_source": source,
    }


def agent_through_ssh(agent: str, source: str, title: str, lines) -> tuple:
    """A local ssh process is not the agent. The terminal UI can be.

    Process evidence that the program is ssh stays distinguishable from
    a Codex binary on this machine. The source becomes ``ui-via-ssh``.
    """

    if source == "process" and agent == "Shell":
        from_ui = _ui_agent(lines or ())
        if from_ui:
            return from_ui, "ui-via-ssh"
        from_title = _title_agent(title or "")
        if from_title:
            return from_title, "ui-via-ssh"
    elif source in ("ui", "title"):
        return agent, "ui-via-ssh"
    return agent, source


def peer_topology(observer: str, peer_display: str) -> Dict[str, str]:
    """A pane that belongs to another host's own tmux server.

    This is not the SSH client pane on the observer. Write stays disabled
    because the row is a read-only snapshot.
    """

    return {
        "observer_host": observer,
        "tmux_host": peer_display,
        "execution_host": peer_display,
        "transport": "local",
        "transport_target": "",
        "topology_source": "peer",
    }
