# Changelog

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
