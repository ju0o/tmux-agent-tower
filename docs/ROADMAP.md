# Roadmap

v0.1.0 covers P0-P4 (TUI stability, status engine v2, project
auto-discovery, a minimal multi-host prototype, and the Workspace
Launcher) plus a Korean-first UI (see "i18n" below). v0.2.0 covers P6
(task awareness) and the "genuine status transition" half of P7
(notifications) -- see below for what's still open in each.

## Workspace Launcher vs. Action Layer -- the distinction that matters here

Earlier drafts of this roadmap classified the Workspace Launcher as part
of the (deliberately unbuilt) Action Layer, reasoning that both involve
starting an agent process. On review, that conflated two different trust
boundaries and was corrected:

* **Workspace Launcher** (P4, implemented): the user explicitly picks a
  host, one or more projects, and an agent from a menu, reviews a preview
  screen, and presses Enter to create **brand-new** panes running that
  agent. Nothing that existed before the launcher ran is touched.
* **Action Layer** (P5, still not implemented): the program acting on an
  **already-running** agent it did not just create -- sending it a prompt,
  `tmux send-keys`, interrupting, restarting, killing, or auto-approving
  a permission prompt on its behalf.

The Workspace Launcher's hard rule (enforced in
`launcher/spawn.py`'s module docstring and its tests) is that it only ever
creates new windows/panes; it never sends keys into, closes, or
reconfigures a pane that existed before it ran. That is a meaningfully
smaller trust boundary than the Action Layer, which is why P4 shipped
while P5 remains a deliberate non-goal for now.

## P5 - Action layer (not started, needs explicit design/approval first)

* Sending a prompt into a monitored (pre-existing) pane.
* `tmux send-keys` automation against a live agent.
* Interrupting, restarting, or killing an agent process.
* Auto-accepting approval/permission prompts.

## P6 - Task awareness (v0.2.0, mostly done)

* Done: a one-line "what is this agent currently doing" guess per pane,
  extracted from already-captured terminal text only (no LLM calls, no
  structured/persisted data -- see `adapters/*.py`'s `extract_activity`).
  Confidence-gated: a low-confidence guess is computed and tested but
  never shown.
* Done: status duration (`StatusEngine.duration_seconds`) -- how long a
  pane has continuously held its current status.
* Not done: "completion / failure inference" -- deliberately still out of
  scope. Tower can observe that a pane *stopped changing*, not that a
  task *succeeded*, *failed*, or is meaningfully "done"; see the
  WORKING → IDLE non-notification rule under P7.

## P7 - Notifications (v0.2.0, partially done)

* Done: an optional, off-by-default, one-shot desktop notification
  (`notify-send`, falling back to `tmux display-message`) on a genuine
  status *transition* into WAITING or DEAD (each independently
  toggleable in `[notifications]`).
* Deliberately not done: a "long-running task finished" notification.
  WORKING → IDLE is explicitly excluded as a notifiable transition --
  Tower has no way to know a task actually finished successfully versus
  the pane just going quiet, and framing "stopped changing" as "done"
  would be exactly the kind of confident-but-wrong signal this project
  avoids.

## P8 - Persistence

* Host/project name mappings that survive a tmux server restart.
* Session recovery hints.

## P9 - Stable v1

## P10 - Optional GUI

## Known limitations from the P4 slice

* Remote (ASUS-style) project selection is manual-path-only: there is no
  remote equivalent of local git-repo auto-discovery yet, since that would
  need its own SSH round trip and wasn't part of this slice. The path is
  validated (`test -d`) over SSH before accepting it.
* Creating a brand-new remote tmux session (when the host had no tmux
  server running at all) leaves tmux's own default first window behind
  alongside the one the launcher created -- cosmetic, harmless, not
  cleaned up automatically.
* The Workspace Launcher's project-picker "recent" list is not scoped per
  host; a path added while targeting one host can appear in another
  host's recent list even though it doesn't exist there. Low-impact since
  it only affects sort order, never spawns anything automatically.

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
