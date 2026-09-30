# Roadmap

v0.1.0 covers P0-P3 (TUI stability, status engine v2, project
auto-discovery, a minimal multi-host prototype) plus a Korean-first UI
(see "i18n" below). None of the following are implemented, and none are
planned without a clear, separate decision to do so -- they represent a
meaningfully larger trust boundary than "read tmux and show it to you."

## P4 - Action layer (not started, needs explicit design/approval first)

* Sending a prompt into a monitored pane.
* `tmux send-keys` automation.
* Interrupting, restarting, or spawning an agent process.
* Auto-accepting approval/permission prompts.
* **Workspace Launcher** (pick a host/project/agent from a menu, create a
  new pane, `cd` into the project, and start that agent CLI there). This
  was proposed as a dogfood convenience, but it is still, mechanically,
  "spawn an agent process" -- the exact thing this list forbids without a
  separate decision to cross that line. Not implemented; revisit
  deliberately, not as a side effect of a UI-wording change.

## P5 - Task awareness

* Extracting "what is this agent currently working on" as structured text.
* Elapsed time / completion / failure inference.

## P6 - Notifications

* Desktop/terminal-bell notification when a pane becomes WAITING.
* Notification when a long-running task finishes.

## P7 - Persistence

* Host/project name mappings that survive a tmux server restart.
* Session recovery hints.

## P8 - Stable v1

## P9 - Optional GUI

## i18n

The dogfood default is Korean-only (`src/tmux_agent_tower/i18n/`): all
user-facing TUI text goes through `i18n.t(key)`, while internal status/visit
constants (`STATUS_WORKING`, `VISIT_NEW`, ...) stay English identifiers so
business logic never compares against translated strings. `en.py` is
already fully populated as the fallback catalog and as the second locale
for the eventual public release, but there is no language picker or
`tower config` yet -- language currently only comes from
`~/.config/tmux-agent-tower/config.toml`'s `language = "ko"` /
`language = "en"` key, read once at startup, defaulting to `ko`. A
first-run KO/EN picker and `tower config` / `tower --lang` are public-release
work, done after Public Gate, not before -- see the priority order at the
top of this file.

## Relationship to `actl`

`actl` is a separate, on-hold personal project. Tmux Agent Tower does not
depend on it and is not a public wrapper around it. If Tmux Agent Tower
eventually grows into P4+, there may be a future decision to consolidate
overlapping functionality -- not part of this roadmap today.
