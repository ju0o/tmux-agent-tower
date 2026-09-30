# Changelog

## v0.1.2 - Identity editing, and a Unicode input bug fix

* **`E` is now a full edit menu**, not just project rename: project name,
  agent label (from the known list or freely typed, e.g. `Gemini CLI`),
  or pane title -- plus "reset to auto-detection" for just that one pane.
  The agent-label override is purely display metadata; it never touches
  the real process (same boundary as the Workspace Launcher -- see
  `docs/ROADMAP.md`).
* Overrides are now stored per-field (`{project, agent, title}` per pane)
  instead of a single project-name string; existing v0.1.0/v0.1.1
  overrides are migrated automatically the first time they're read, in
  place, with nothing lost.
* Pane title edits are best-effort applied to the pane's real tmux title
  too (`select-pane -T`); a failure there can't take the rest of Tower
  down with it.
* **Fixed a real, pre-existing Unicode input bug**, caught live while
  testing the Korean pane-title editor: every text input in the TUI
  (project/agent/title prompts, the Workspace Launcher's search box and
  manual-path entry) read keys one raw *byte* at a time
  (`curses.getch()`), which silently mangled any multi-byte UTF-8
  character -- e.g. typing "개발" came out as "ê°ë°" in a real pane
  title. Switched every input loop to `curses.get_wch()` (locale-aware,
  assembles a full character before returning it) plus
  `locale.setlocale(locale.LC_ALL, "")` at startup. This affected typing
  *any* non-ASCII text anywhere in the app, not just this new feature.

## v0.1.1 - Hotfix: pane-identity navigation bug

Real-world dogfooding of v0.1.0 found that `tower` would sometimes jump to
the wrong window: it identified "the" Tower by a window named `CONTROL`,
and a window keeps its name even after being renamed or reused for
something completely unrelated.

* **Breaking change to `tower`'s default behavior**: running `tower` now
  always runs the TUI in the current pane (what `--here` used to do).
  It no longer creates or jumps to a dedicated `CONTROL` window.
* Added `tower --focus`: jumps to whichever pane is currently the active
  Tower for this tmux session, without starting a new one. This is what
  an optional `Ctrl+b w`-style binding should call now (see README) --
  plain tmux navigation, no curses, safe to background.
* Tower now identifies itself by pane_id, registered in a session-scoped
  tmux option, verified alive before ever being used as a navigation
  target -- never by matching a window's name. Starting a second Tower in
  another pane makes it the new focus target without killing the first.
* Fixed a related bug in how Tower found its own pane_id at startup
  (`tmux display-message` without a target reports the attached client's
  *currently active* pane, not necessarily the pane the process is
  actually running in -- now reads `$TMUX_PANE` instead, which has no
  such ambiguity).
* The same window-name matching bug also affected self-exclusion (Tower
  hiding its own pane from its own list) -- fixed the same way, which as
  a side effect now correctly shows other panes that happen to share a
  window with Tower (previously the whole window was hidden).
* `--here` is now a deprecated no-op (same as running with no flags).

## v0.1.0 - Early preview

Initial prototype release.

* Curses TUI with host-grouped pane list, live refresh, pane navigation.
* Status engine v2: WORKING / WAITING / IDLE / UNKNOWN / DEAD, fully
  decoupled from NEW / SEEN visit tracking.
* Adapters for Codex, Claude Code, OpenCode, Grok CLI, Cursor Agent CLI,
  and a generic shell/SSH fallback (best-effort; see README limitations).
* Git-root-based project auto-discovery, with a persistent manual override
  ("E" to rename) that survives future refreshes.
* Minimal SSH-based multi-host prototype (read-only, graceful offline
  degradation, nothing installed on the remote host).
* `tower --doctor` environment diagnostics.
* Installer/uninstaller scripts, test suite, CI.
