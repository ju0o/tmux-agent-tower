# Architecture

```
src/tmux_agent_tower/
├── adapters/       agent-specific status opinions (Codex, Claude, ...)
├── detection/      status engine, project auto-discovery, process helpers
├── tmux/           thin wrappers around the `tmux` binary (read + navigate)
├── state/          NEW/SEEN visit tracking, custom title overrides
├── remote/         minimal SSH-based multi-host prototype
├── ui/             the curses TUI itself
└── main.py         `tower` CLI entry point
```

## Data flow, one refresh cycle

```
tmux list-panes  ──▶  discovery.list_panes()  ──▶  for each pane:
                                                       adapters.resolve_adapter()
                                                       detection.status.StatusEngine.evaluate()
                                                       state.visits.VisitStore.visit_label()
                                                       state.overrides.OverrideStore.get()
                                                     ──▶  display row
remote/collector (per configured host, throttled)  ──▶  display rows
                                                     ──▶  ui.tower.draw()
```

Nothing in this path sends input to a monitored pane or touches any
process other than `tmux` itself, `ps` (read-only process listing), and
`ssh` (for the optional remote prototype, also read-only on the far end).

## Why a local `tower --collect-json`-on-the-remote-host design was *not*
chosen for P3

The simplest possible multi-host design would install this same package on
the second machine and have it print a JSON snapshot for the first machine
to fetch over SSH. That was deliberately avoided for the v0.1.0 prototype:
it would mean silently installing software on a machine that might have
its own live, unrelated agent sessions running, which conflicts with this
project's "never touch a machine you weren't explicitly asked to touch"
default. Instead, `remote/collector.py` runs a small inline shell snippet
over the user's *existing* SSH alias that only calls `tmux list-panes`
(nothing installed, nothing written, one round trip). The tradeoff is a
coarser remote status (title-only, see `docs/STATUS_ENGINE.md`) in exchange
for zero remote footprint. This can be revisited later if a richer remote
view is wanted badly enough to justify the extra installation step.

## Extension points

* **New agent**: add `adapters/<name>.py` implementing `AgentAdapter`,
  register it in `adapters/__init__.py`'s `ADAPTERS` list order (order
  matters: first match wins), add fixtures + tests.
* **New host**: add a line to `~/.config/tmux-agent-tower/remote-hosts.txt`
  (`alias` or `alias:Display Name`), using an SSH alias you've already set
  up in `~/.ssh/config`. No code changes needed.
