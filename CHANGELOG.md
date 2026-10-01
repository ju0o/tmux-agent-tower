# Changelog

## Unreleased - Tower Remote (`feat/tower-remote` branch only, not on `main`)

Experimental phone-browser companion to the TUI. Same data, same
adapters, same status engine -- `server/httpapi.py` drives a headless
`Tower` instance; nothing is re-implemented. Full scope, security model,
and known gaps in `docs/REMOTE.md`. Version number is intentionally not
bumped until this merges.

* **Attention** -- execution (`WORKING` / `IDLE` / `UNKNOWN` / `DEAD`),
  attention (`none` / `approval_required` / `input_required` / `error`),
  and result (`none` / `ready` / `read`) stay separate. Each agent
  adapter detects its own bottom widget. A Codex command menu
  (`1. Yes` / `3. No`, including "Would you like to run the following
  command?") sends `1` or `3` only. The live trust screen's footer is
  `enter continue` / `esc quit`, so those named keys are sent instead of
  the digit `1`, which did not move that widget. Claude's numbered
  "Do you want to proceed?" menu sends `1` or `3`. Claude's folder
  picker, OpenCode, Cursor, Grok, and unknown agents show
  "승인은 실제 Pane에서 처리하세요" and send nothing. Phone cards sort
  approval, then input, then a new result.
* **Pane control** -- Enter stays in Tower and opens a live view of that
  pane. `P` prompt, `E` identity, `Y` result, `G` focus, and `X` close
  (confirm first; Tower's own pane is refused) all go through one action
  layer shared with the phone. `G` is the only move.
* **tmux navigator** -- `V` groups local panes by session and window.
  Windows are dim divider lines, not rows: the cursor only lands on
  panes and Enter always opens exactly one pane id. The phone list does
  the same: a window is a non-clickable "창 N: name" label, and only a
  pane card opens detail. Stale ids refresh instead of moving. The phone
  detail shows the same location and moves the PC only from
  **PC를 이 Pane으로 이동** (`POST /api/panes/<key>/focus`), never from
  a card tap, and never onto an SSH host.
* **`tower serve`** -- pairing-gated read API (`/api/status`) + mobile
  web UI, localhost-only by default. `--lan` binds all interfaces for
  same-Wi-Fi use (plain HTTP; documented as trusted-LAN-only).
* **One-pane prompt send** (`/api/prompt`) -- explicit pane, typed text, and an explicit tap:
  explicit pane, explicit typed text, explicit tap, paired token, and a
  live stale/wrong-pane re-check before anything is sent. No exec, no
  filesystem, no raw transcript, no interrupt/kill.
* **Input transport + submit verification** -- the text goes in as one
  bracketed paste (`tmux load-buffer -` from stdin, then
  `paste-buffer -d -p`) followed by exactly one Enter. Measured live on
  Codex 0.159.3, Claude Code 2.1.283 and OpenCode 1.18.32: `send-keys -l`
  plus an immediate Enter left the text sitting in Codex's composer
  (the Enter became a newline in the paste burst), while bracketed paste
  plus one Enter submitted short, Korean, 1200-char and multiline/code
  prompts on all three. Before sending, Tower snapshots the pane; after
  the single Enter it watches for up to 3s for adapter-specific evidence
  (Codex: Working line or `›` echo plus idle hint; Claude: active verb or
  a new "<Verb>ed for Ns" summary; OpenCode: `esc interrupt` or a new
  `▣ ... Ns` line). The response carries `submitted: true|false` and
  `reason: submit_not_confirmed`; it never retries Enter and never fakes
  success. Phone and TUI share `control.actions.send_text` /
  `submit_input` / `send_prompt`. The phone button shows "전송 중..." then
  "제출 확인됨" or "제출 확인 실패", blocks a second tap while in flight,
  and clears the textarea only when the submit was confirmed.
* **Phone page fixes** -- the pairing screen's confirm text held a raw
  newline that broke the whole inline script (the page never polled or
  sent), and an unauthenticated status poll fired from the pair screen
  could return 401 right after a successful pair and hide the dashboard
  again. Polling now starts only once a token exists.
* **Claude Code 2.1.x detection fixes** -- the pane title is a static
  "✳ <conversation title>" while idle *and* while working, so it is no
  longer read as a spinner; idle is read from the empty/placeholder box or
  the "? for shortcuts" / "← for agents" footer; the finished-turn summary
  uses any verb ("Brewed/Crunched/Cooked ... for Ns"), not only "Cooked",
  and the Result body is the last turn only. `capture_pane` drops trailing
  blank rows so tall detached windows (OpenCode footer 40+ rows above the
  bottom) classify like normal panes.
* **Pairing hardening** -- 6-digit single-use 5-minute code, 7-wrong-guess
  lockout with an indistinguishable `invalid_code` response, local-only
  `r` + Enter regeneration (no HTTP path), tokens at `0600` in a `0700`
  config dir, `Host` header allowlist, 4000-char prompt / 8KB body caps.
* **`tower serve --tailscale`** -- off-LAN access over your tailnet
  without ever exposing the port: the backend stays on `127.0.0.1`, and
  Tower drives the *Windows-host* `tailscale.exe` (WSL2 layout; no
  Tailscale inside WSL) to create a tailnet-only HTTPS Serve mapping to
  `localhost:<port>`. Verifies Windows -> WSL reachability with `curl.exe`
  *before* touching Serve config and fails closed if it can't; reads the
  MagicDNS name from `tailscale status --json`; refuses to overwrite an
  existing unrelated `:443` mapping; removes on Ctrl+C only the mapping
  that still points at its own backend, via the per-port
  `serve --https=443 off`. `tailscale funnel` and `tailscale serve reset`
  are never invoked (regression-tested). MagicDNS name and Tailscale IPv4
  are added to the `Host` allowlist -- never a wildcard. `--lan` and
  `--tailscale` are mutually exclusive. Live-verified on a real Windows
  Tailscale 1.102.2 + WSL2 setup: HTTPS health / pair / authenticated
  status over the tailnet URL, localhost-only bind confirmed with `ss`,
  direct `http://<tailscale-ip>:4312` correctly unreachable, mapping
  cleaned up on stop. Phone-on-LTE acceptance is the remaining manual
  step.
* **Mobile pane detail + LIVE PANE** -- a card opens a detail screen
  (it does not send). The screen is recent plain text from
  `tmux capture-pane` (`GET /api/panes/<key>/screen`): bearer required,
  local panes in the bound session only, ANSI stripped, last 60 lines /
  400 chars / 16KB, `Cache-Control: no-store`, not written to disk or
  logs. The phone polls about once a second (up to two if a round trip
  is slow) and pauses while the tab is hidden. SSH/remote keys are
  `remote_unsupported` and the page says live view is not available
  there. Project, agent, and title edits use the same `OverrideStore`
  as the TUI, which reloads the file on change; a local title is also
  `select-pane -T`. Reset clears the override.
* **Phone result awareness** -- status and result stay separate.
  Codex, Claude Code, OpenCode, and Cursor can mark `ready` only from
  their own completion UI plus a prose body. Idle time and the words
  "Done"/"Finished" are not enough. `GET /api/panes/<key>/result`
  returns that body in memory (`no-store`); the card shows `✓ 새 Result`
  and the phone copies just that text. Shell and Grok do not invent a
  result. The status engine used by Remote is shared across polls so
  WORKING does not flap every request.
* **Smart `Ctrl+b w` (opt-in)** -- `C` → tmux shortcut, or
  `tower keys install`, writes one marked block in `~/.tmux.conf`
  (backup once, no full-file rewrite). The binding stays put: `tower
  --has-active` exits 0 only for a live registered pane id, and the
  key then runs `tower --focus`; otherwise it runs the `w` command
  recorded at install time (a Tower-owned binding is not recorded;
  the fallback is queried from a tmux server with an empty config).
  `tower keys restore` removes only that block. Tower start/stop does
  not rebind the key. The public installer still does not opt in.
* Tests: 225 (v0.2.2) -> 353, then 421 after live pane and smart `Ctrl+b w`, then 435 after result awareness.
* **In-TUI remote control (`M`)**: `tower` then `M` starts, inspects,
  and stops the phone remote. No shell command is shown. The header
  badge comes from a real health check plus the Tailscale Serve mapping,
  not a local flag. The server is a detached process of the same
  `server.service` module the CLI uses (no `shell=True`); quitting the
  TUI with `Q` leaves it running, and stop refuses to signal a PID that
  is not Tower Remote. `[remote] autostart` (default off) can start it
  with the TUI; a failure shows `원격: ! 오류` and the TUI still opens.
  Pairing codes are regenerated from the TUI through a local control
  file, not an HTTP endpoint. Paired devices can be listed and
  disconnected; older token files (no label) still load.
* **Fix: remote bound to an explicit tmux session.** The detached server
  used to re-infer its session from its own environment; started from a
  since-deleted scratch session it kept serving an empty local list (the
  phone saw only the SSH host). Now the starting Tower/CLI passes
  `session` + `own_pane_id` explicitly, the runtime file records them,
  `status()` also checks the session still exists (otherwise
  `error/source_session_missing`, never `● 연결됨`), `/api/status`
  returns a structured `source_session_missing` error instead of
  `panes: []`, the `M` menu shows `관제 세션:`, and starting from a Tower
  in another session offers "switch / keep / cancel" instead of a silent
  "already running". Autostart binds to the opening Tower's session and
  never guesses. Tests: 353 -> 384, including a real-tmux scratch-session
  lifecycle test on a private socket.

## v0.2.2 - Main stabilization checkpoint

No feature changes. This is the tagged baseline the `feat/tower-remote`
work branches from. Ran the full stabilization pass: full test suite
(225 passing), a live dogfood re-check of every shipped feature (status
detection, identity editing, search filter, Attention View, task
awareness, and the notification pipeline end-to-end including the
`tmux display-message` fallback) in an isolated tmux session, and a
docs-consistency check. Found and fixed one remaining stale spot:
`docs/ARCHITECTURE.md`'s module tree and per-refresh data-flow diagram
predated Task Awareness (missing `notify.py`, `extract_activity()`,
`duration_seconds()`, and `NotificationTracker.observe()`).

## v0.2.1 - Documentation consistency fix

Docs-only patch, no code changes. `docs/ROADMAP.md`'s "Known limitations
from the P4 slice" section had been stale since v0.1.0 itself: all three
listed gaps (remote project selection being manual-path-only, a
leftover default window on a brand-new remote session, and the "recent"
project list not being scoped per host) were actually already fixed in
the same P4 commit that introduced the Workspace Launcher -- the fixes
just never made it back into this doc, and no CHANGELOG entry ever
mentioned them either. Corrected the ROADMAP section to point at the
actual implementing code and tests instead of describing gaps that don't
exist. No functional or behavioral change; 225 tests unchanged.

## v0.2.0 - Task awareness: activity, duration, attention view, notifications

Tower could tell you an agent was WORKING, but not *at what*, or *for how
long*, and there was no way to jump straight to the panes that actually
need you. This slice adds all three, plus optional, off-by-default
notifications -- with a hard rule carried through every part of it:
**an honest "no answer" beats a plausible-sounding wrong one.**

* **Current-activity line**, one line per pane, extracted purely from
  already-captured terminal text -- no LLM calls, nothing sent anywhere.
  Codex reads its own step bullets (preferring an in-progress "Calling
  ..." line); Claude reads its active-verb status line, with a low-
  confidence bullet fallback; Grok reads its `Task ... (n) Ns` line;
  OpenCode reads its `~ Preparing ...` / `→ ...` lines. Cursor and Shell
  report no opinion -- there's no reliable evidence for either. Anything
  below "medium" confidence is computed (and tested) but never shown.
  Raw terminal text is never persisted to disk; activity strings exist
  only in memory for the current draw.
* **Status duration**: "Tower has observed this status continuously for
  N" (`StatusEngine.duration_seconds`), shown next to the status text and
  in the detail panel. Framed as an observation window, never as "the
  agent started N ago" -- Tower doesn't know when the agent actually
  started.
* **Attention View** (`A`): re-sorts the flat pane list by
  WAITING > UNKNOWN > DEAD > WORKING > IDLE without ever touching the
  default per-host list order -- it's a toggle, not a replacement.
* **Notifications, off by default**: a genuine status *transition* (not
  "still WAITING") can fire a one-shot desktop notification via
  `notify-send`, falling back to `tmux display-message` wherever there's
  no notification daemon (confirmed working end-to-end in a plain
  WSL/tmux session with no desktop at all). WORKING → IDLE is deliberately
  **not** a notifiable transition -- Tower cannot know a turn is actually
  "done," only that the pane stopped changing. `waiting`/`dead` are
  independently toggleable in `[notifications]`.
* **New config.toml keys**: `show_activity`, `show_status_duration`,
  `notifications` (top-level bool), and a `[notifications]` section with
  `waiting`/`dead` booleans -- all default to the conservative/quiet
  choice (activity and duration on, notifications off).
* **Detail panel** gained a "current activity" field (hidden when there
  is none, per the confidence rule above), and its status field now
  includes the duration suffix.
* **Fixed four real bugs, all caught by dogfooding against live panes,
  not by code review**: (1) all four adapters initially scanned only the
  last 20 captured lines for activity; a short reply can leave enough
  blank padding below it that the real activity line falls outside that
  window on a real pane, so every adapter now scans the full capture.
  (2) Codex's own "• Working (...)" and "• Finished ..." bullets were
  being read back as if they were a task description. (3) OpenCode's
  `→ ...` line survives verbatim into the idle state from the last
  finished turn, so it's now only trusted when a `working` marker is
  also present. (4) **Found after this file's own dogfood check against
  a real, already-idle Codex pane** (not a synthetic fixture): a
  finished turn leaves its multi-bullet summary sitting in scrollback
  right above the idle prompt, and the "last bullet found anywhere"
  fallback was reporting that stale, completed summary as current
  activity. Fixed by requiring the same "Working (...)" evidence line
  before trusting *any* bullet, not just the medium-confidence fallback.
* Also fixed a config-parsing collision where a top-level `notifications`
  boolean and a `[notifications]` section both wrote into the same
  internal key, and a status-engine bug where DEAD duration could never
  accumulate past one refresh tick (it was being reset to zero on every
  single DEAD observation instead of only on a pane-id-reuse transition).
* 225 tests total, including new dedicated suites for activity
  extraction (`test_activity.py`) and notifications (`test_notify.py`),
  plus new duration/attention coverage in the existing status-engine and
  render suites.

## v0.1.3 - Compact row UI redesign

The pane table felt like a spreadsheet, not a control tower. Redesigned
the main screen around "can I read this in 1-2 seconds," not fixed
columns.

* **Compact rows** instead of a wide fixed-column table: project name is
  the primary text, agent + status share the same line (or the next line
  down on a narrow terminal), and the pane's own title appears as a
  second line only when it says something the project name doesn't
  already.
* **VISIT is no longer a column.** An unvisited pane gets a small `NEW`
  marker in front of it; a visited one shows nothing extra.
* **Host summary hides zero counts** (`MAINPC ● 3  ○ 4` instead of
  `MAINPC ●3 !0 ○4 ?0 ×0`) and moved to the title line, right-aligned per
  host.
* **New selected-item detail panel** at the bottom (project, agent, pane
  title, path, status, host, pane id) -- auto-hidden on a short terminal
  so the pane list keeps the room instead.
* **Live search filter** (`/`, then type; `Esc` clears) across project,
  agent, pane title, path, and host.
* **Smarter project-name fallback**: a real priority chain (your own
  override -> the enclosing git repo's name -> a meaningful pane title ->
  the raw directory basename -> an honest "no name") means a project run
  straight from a drive root no longer shows up as a single meaningless
  letter (`f`) when anything better -- a git repo name or a real pane
  title -- is available.
* **Edit menu** (`E`) now shows the current override and the
  auto-detected value side by side ("현재: X" / "자동: Y") before you type
  a replacement, so it's obvious whether you're overriding something or
  just confirming it.
* **Fixed two real display bugs**, both caught live once rows started
  mixing Korean and English text on the same line: (1) Korean (and other
  East-Asian-wide) characters are double-width on screen but were counted
  as single-width everywhere -- host-summary text got clipped, detail
  panel labels misaligned, and narrow-layout hint text wrapped onto the
  next terminal line instead of being clipped. Fixed with a proper
  `display_width`/`truncate_to_width` (East Asian Width aware) used by
  every text-drawing call, not just the spots that happened to trigger
  it. (2) The "is this pane title just a generic default" check compared
  against the user's *chosen* display host label instead of the actual OS
  hostname, so a plain auto-titled pane wasn't recognized as generic when
  the user had renamed their host's display label away from its raw
  hostname.
* Responsive: narrow terminal drops agent/status to their own line; short
  terminal hides the detail panel; stress-tested resizing across sizes
  from 1x1 to 300x100 with no crash.

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
