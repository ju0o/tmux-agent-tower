# Status Engine v2

## Why this document exists

The original throwaway prototype had one serious bug: it used a single
"CHECKING" state to mean *both* "haven't determined a real status yet" and
"the user hasn't looked at this pane yet." That meant a pane where an agent
was actively working could be shown as "CHECKING" forever if nobody had
opened it -- silently hiding real activity. This document describes the
fix and the rules that prevent it from happening again.

## Independent state axes

* **Execution**: `WORKING`, `IDLE`, `UNKNOWN`, `DEAD`. Answers what the
  current pane evidence says about execution.
* **Attention**: `none`, `approval_required`, `input_required`, or `error`.
  It describes a current interaction and does not change execution state.
* **Result**: `none`, `ready`, or `read`. It describes the newest completed
  answer and does not change execution or attention state.
* **Visit**: `NEW` or `SEEN`. It records whether Tower has opened the pane.

These values are evaluated and stored separately, then combined for display.
`WAITING` is a legacy UI/notification projection for attention, not an
execution state. An idle pane can therefore also need approval or input, or
have a ready/read result.

## Evaluation order (per pane, per refresh)

1. **Dead** -- pane/process is gone. Always wins.
2. **Adapter says WORKING** -- an agent-specific current-turn signal (e.g.
   Codex's `Working (... esc to interrupt)` line, Claude's active-verb
   spinner line). Refreshes the hold window (see below).
3. **Current idle or interaction widget** -- a composer/footer ready for
   input, or a recognized current approval/question prompt, reports IDLE
   execution. Attention remains `approval_required` or `input_required`.
   A clock tick under that widget is not work, so it does not count as
   output and it clears the hold. Adapter detectors must let a newer
   current widget override older scrollback markers.
4. **Screen content changed** since the last observation -- WORKING for
   this refresh and refreshes the hold window. Adapters first resolve
   conflicting indicators against the current widget: an idle composer or
   current footer below an older running marker wins. OpenCode's working
   footer can show both running and idle phrases on the same line; that row
   is treated as WORKING.
5. **Recently active** -- if WORKING was true within the last
   `hold_seconds` (default 8s), keep reporting WORKING even though the
   screen just paused. Without this, a pane that stalls for a second
   between tool calls would flicker WORKING -> IDLE -> WORKING every
   refresh, which is worse than useless. An idle widget already cleared
   this hold.
6. **First observation, or blank content** -- honestly `UNKNOWN`. We have
   not established a baseline for this pane yet, or there's nothing to
   reason about.
7. **Stable, non-blank, no adapter opinion** -- `UNKNOWN`. Silence is not
   evidence of IDLE. Approval, questions, and results remain independent
   axes; an unrecognized interaction exposes no guessed key.

Status history is scoped by pane ID and the tmux pane process PID. If that
runtime identity changes or goes missing, the old screen hash and working
hold are discarded before evaluating the new observation.

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
  A fixture test proves detector behavior for that text shape only; it does
  not establish live support when the adapter's capture is marked unverified.
* Remote (multi-host) panes currently only see the pane **title**, not
  captured content (see `docs/ARCHITECTURE.md`), so remote status is
  coarser than local status -- often `UNKNOWN` for agents whose adapter
  relies on content text rather than a title spinner.
