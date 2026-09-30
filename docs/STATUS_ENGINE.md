# Status Engine v2

## Why this document exists

The original throwaway prototype had one serious bug: it used a single
"CHECKING" state to mean *both* "haven't determined a real status yet" and
"the user hasn't looked at this pane yet." That meant a pane where an agent
was actively working could be shown as "CHECKING" forever if nobody had
opened it -- silently hiding real activity. This document describes the
fix and the rules that prevent it from happening again.

## Two independent axes

* **STATUS**: `WORKING`, `WAITING`, `IDLE`, `UNKNOWN`, `DEAD`. Answers "what
  is this pane doing right now?"
* **VISIT**: `NEW`, `SEEN`. Answers "has a human opened this pane from the
  Tower before?"

`detection/status.py` (the `StatusEngine`) has no parameter, field, or
concept related to visits at all. `state/visits.py` has no parameter,
field, or concept related to status at all. They are combined only in the
UI layer (`ui/tower.py`) when building a display row. This is enforced by a
regression test (`test_unvisited_but_actively_working_pane_reports_working_not_checking`
in `tests/test_status_engine.py`).

## Evaluation order (per pane, per refresh)

1. **Dead** -- pane/process is gone. Always wins.
2. **Adapter says WORKING** -- an agent-specific strong signal (e.g.
   Codex's `Working (... esc to interrupt)` line, Claude's active-verb
   spinner line). Refreshes the hold window (see below).
3. **Screen content changed** since the last observation -- also WORKING,
   also refreshes the hold window. This is the fallback for agents with no
   adapter opinion, and it's why even an unrecognised CLI shows WORKING
   while it's visibly producing output.
4. **Recently active** -- if WORKING was true within the last
   `hold_seconds` (default 8s), keep reporting WORKING even though the
   screen just paused. Without this, a pane that stalls for a second
   between tool calls would flicker WORKING -> IDLE -> WORKING every
   refresh, which is worse than useless.
5. **Adapter says WAITING** -- an agent-specific approval/confirmation
   prompt.
6. **Adapter says IDLE** -- an agent-specific "ready for input" signal.
7. **Generic waiting-prompt fallback** -- for agents with no specific
   adapter opinion, a conservative regex over common confirmation phrasing
   ("do you want to proceed?", "(y/n)", etc).
8. **First observation, or blank content** -- honestly `UNKNOWN`. We have
   not established a baseline for this pane yet, or there's nothing to
   reason about.
9. **Stable, non-blank, no waiting markers** -- best-effort `IDLE`.

## Guiding principle

A wrong `WORKING` or a wrong `IDLE` actively misleads someone deciding
which of ten panes needs their attention right now. A wrong `UNKNOWN`
just means "check this one yourself" -- annoying, but honest. Every rule
above is ordered so that when evidence conflicts or is missing, the engine
degrades toward `UNKNOWN` rather than guessing.

## Known limitations

* Status detection is entirely **best-effort**, based on regexes and
  output-diffing against real captured samples of each agent's TUI. New
  versions of any of these CLIs can change their UI and break an adapter's
  patterns. See `fixtures/` for the samples each adapter was built against,
  and `CONTRIBUTING.md` for how to fix a broken pattern.
* Remote (multi-host) panes currently only see the pane **title**, not
  captured content (see `docs/ARCHITECTURE.md`), so remote status is
  coarser than local status -- often `UNKNOWN` for agents whose adapter
  relies on content text rather than a title spinner.
