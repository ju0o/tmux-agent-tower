# Architecture

```
src/tmux_agent_tower/
├── adapters/       agent-specific status opinions (Codex, Claude, ...)
├── detection/      status engine, project auto-discovery, process helpers
├── tmux/           tmux binary wrappers: read, navigate, and
│                   id-targeted structure changes (local TUI only)
├── state/          NEW/SEEN visit tracking, custom title overrides
├── remote/         minimal SSH-based multi-host prototype (read-only)
├── launcher/       config, discovery, read-only browse, spawn
│                   (browse never creates a pane -- see docs/ROADMAP.md)
├── i18n/           translator layer (ko/en catalogs); UI text only, never
│                   business logic
├── ui/             the curses TUI, workspace browser, and structure menus
├── notify.py       opt-in, off-by-default status-transition notifications
│                   (notify-send, falling back to tmux display-message)
└── main.py         `tower` CLI entry point
```

`launcher/` is deliberately separate from `remote/`: `remote/collector.py`
only ever reads (P3), while `launcher/spawn.py` writes -- it creates new
windows/panes, locally via direct `tmux` calls and remotely via a
generated shell script run once over SSH (see its module docstring for the
exact rules it enforces). `launcher/browse.py` stays on the read side for
both machines: one directory per listing, no tmux until create is
confirmed. Keeping that as its own package makes the read-only vs.
write-capable boundary obvious at the directory level.

## Data flow, one refresh cycle

```
tmux list-panes  ──▶  discovery.list_panes()  ──▶  for each pane:
                                                       adapters.resolve_adapter()
                                                       detection.status.StatusEngine.evaluate()
                                                       detection.status.StatusEngine.duration_seconds()
                                                       adapters.<agent>.extract_activity()  (v0.2.0)
                                                       state.visits.VisitStore.visit_label()
                                                       state.overrides.OverrideStore.get()
                                                       notify.NotificationTracker.observe()  (v0.2.0)
                                                     ──▶  display row
remote/collector (per configured host, throttled)  ──▶  display rows
                                                     ──▶  ui.tower.draw()
```

`extract_activity()` and `duration_seconds()` are pure read/observe
calls over data already captured for status detection -- neither adds a
new source of I/O. `NotificationTracker.observe()` is off by default
(see `launcher/config.py`'s `notifications` key) and, when enabled, can
only ever call `notify.send()` (a `notify-send`/`tmux display-message`
fire-and-forget), never anything that reaches into a monitored pane.

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
