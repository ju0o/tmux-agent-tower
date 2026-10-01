# Tmux Agent Tower

**English | [한국어](README.ko.md)**

See what your coding agents are doing across tmux panes.

If you run Codex, Claude Code, OpenCode, Grok CLI, or Cursor Agent CLI in
several tmux panes at once (maybe across more than one machine), it's easy
to lose track of which one is still working, which one is stuck waiting
for your approval, and which one finished ten minutes ago. Tmux Agent
Tower is a small, local-first TUI that lists them all in one screen and
jumps you straight to the one that needs you.

![Tmux Agent Tower screenshot: a host-grouped pane list showing status, agent, and visit columns](docs/assets/demo.png)

*(mockup with generic placeholder project names; the Korean UI is the
current default -- see [Language](#language) below)*

```
TMUX AGENT TOWER                                    MAINPC ● 2  ○ 1   ASUS ○ 1

── MAINPC ──────────────────────────────────────────────────────────────────
> ● alpha-api                                        Codex          WORKING
    Marketplace build
NEW ! web-app                                        Codex          WAITING
  ○ docs                                             Grok           IDLE

── ASUS ────────────────────────────────────────────────────────────────────
  ● billing-service                                  Claude         WORKING

↑↓ Move  Enter Open  E Edit  N Add  W Workspace  M Remote  C Settings  / Search  A Attention  R  Q
```

## What it is (and isn't)

* **Local-first**: no server, no account, no cloud backend. It reads your
  own tmux server (and, optionally, a second host over your own SSH
  connection) and shows it to you.
* **Read-only by default**: it never sends keystrokes into a monitored
  pane, never kills or restarts a process, and never touches your actual
  agent CLIs or their credentials. It only reads tmux state and moves
  *your own* cursor between panes when you press Enter -- the same thing
  `Ctrl+b` + arrow keys would do.
* **Best-effort status**, not a guarantee. Status is inferred from
  terminal output patterns and process activity, not each agent's
  internal API (most don't expose one). New versions of an agent's CLI can
  change its UI and break detection for that agent -- see
  [`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md).

## Features

* One screen, grouped by host, with a compact row per pane (project,
  agent, status, and -- when it says something the project name doesn't
  already -- the pane's own title as a second line) instead of a wide,
  sparse table.
* Status and "have I looked at this yet" are tracked completely
  separately -- an agent working away in a pane you haven't opened yet
  correctly shows `WORKING`, not some vague "checking" placeholder. A
  small `NEW` marker (not a whole column) flags an unvisited pane; a
  visited one shows nothing extra.
* A selected-item detail panel (project, agent, pane title, path, status,
  host) fills in anything the compact row had to leave out -- hidden
  automatically on a short terminal.
* `/` live-filters the list by project, agent, pane title, path, or host;
  `Esc` clears it.
* Project name is auto-discovered with a real priority chain: your own
  override, then the enclosing git repo's name, then a meaningful pane
  title, then the directory name -- a meaningless single-letter or
  mount-point-ish basename (`f`, `mnt`, ...) is never shown as-is if
  anything better is available.
* Per-host summary counts hide any status with a zero count instead of
  spelling out `?0 ×0` for statuses that aren't happening.
* Works across a second host over SSH, degrading gracefully to
  `UNKNOWN`/offline if that host isn't reachable -- the local TUI never
  blocks waiting on it.
* A one-line **current activity** guess per pane (e.g. `Calling
  some_tool...`, `~ Preparing write...`), extracted purely from
  already-captured terminal text -- no LLM calls, nothing leaves your
  machine, and raw terminal content is never written to disk. Shown only
  when there's real evidence for it; hidden rather than guessed when
  there isn't.
* **Status duration**: how long Tower has continuously observed a pane in
  its current status (`대기 · 4m`), shown next to the status and in the
  detail panel.
* **Attention View** (`A`) re-sorts the list by what needs you first --
  approval, then a question, then a new result, then an error, then
  `WORKING > IDLE > UNKNOWN > DEAD` -- without changing the default
  per-host order; press `A` again to go back. Execution, attention, and
  result stay separate fields.
* Optional, **off-by-default** notifications on a genuine status change
  (e.g. an agent starts waiting on approval or a question), via `notify-send` or a
  `tmux display-message` fallback wherever there's no desktop
  notification daemon. One-shot per transition -- no repeat spam while a
  pane just sits in the same status.

## Install

Requires Python 3.9+, tmux, and a terminal `TERM` that supports curses
(any normal one does).

```bash
git clone https://github.com/ju0o/tmux-agent-tower
cd tmux-agent-tower
./scripts/install.sh
```

The installer:

* installs the `tower` command for your user (`pip install --user -e .`,
  or into a venv if `pip install --user` is externally managed),
* appends a single `source` line to your `~/.bashrc` (only if not already
  present),
* backs up any dotfile it touches first (`<file>.bak.<timestamp>`),
* does **not** touch `~/.tmux.conf` or rebind any tmux key by default --
  see "Optional: `Ctrl+b w` shortcut" below.

Run `./scripts/uninstall.sh` to remove what it added (it will tell you
exactly what it's about to undo before doing it).

## Usage

Inside any tmux pane:

```
tower
```

runs the TUI right there, in that pane. Press `M` → **Start phone
remote** when you want the same view on your phone (no shell command).
If you start another Tower in a different pane later, that one becomes
"the" active Tower -- older ones
keep running, nothing is killed, but `tower --focus` (see below) will jump
to the newest one.

Tower remembers which pane it's running in for the rest of that tmux
session (by pane id, not by window name or position -- a renamed or
reused window never confuses it). From anywhere else in that session:

```
tower --focus
```

jumps straight back to it without starting a new one. This is what the
optional `Ctrl+b w` shortcut below is for -- it's a plain navigation
command, not another TUI, so it's safe to bind to a key.

| Key | Action |
|---|---|
| `↑` / `↓` (or `j`/`k`) | Move selection |
| `Enter` | Jump to the selected pane |
| `E` | Edit the selected pane's project name, agent label, or pane title |
| `N` | Add one project (Workspace Launcher, single) |
| `W` | Start a new workspace (Workspace Launcher, multi) |
| `M` | Phone remote: start, show the address and pairing code, or stop |
| `/` | Live-filter the list (project/agent/title/path/host); `Esc` clears it |
| `A` | Toggle Attention View (sorts by what needs you first) |
| `R` | Refresh immediately |
| `Q` / `Ctrl+C` | Quit the TUI (a phone remote you started keeps running) |

### Editing what's shown (`E`)

Auto-detection is best-effort and sometimes wrong or just unhelpful (a
project run straight from a drive root can show up as a single letter;
an agent that isn't recognised shows as `Shell`). `E` opens a small menu
for the selected row:

* **Project name** -- overrides the auto-discovered project name.
* **Agent name** -- overrides the displayed agent label, either picked
  from the known list or typed freely (e.g. `Gemini CLI`). This is
  **display-only metadata**: it never changes, restarts, or sends
  anything to the real process -- it just relabels the row.
* **Pane title** -- also applied to the pane's real tmux title
  (`select-pane -T`) on a best-effort basis; a failure there never takes
  the rest of Tower down with it.
* **Reset to auto-detection** -- clears all three overrides for *this*
  pane only (not a global reset).

Overrides persist across refreshes and are stored separately per pane, so
auto-discovery running again next refresh never clobbers a choice you
made here.

### Workspace Launcher (`N` / `W`)

Picks a host, one or more projects (auto-discovered from your configured
project roots, searchable, or a manual path), and an agent per project,
shows a preview you can adjust (per-project agent override, layout), and
then creates **brand-new** tmux panes for them -- `cd`'d into the project,
running the chosen agent, titled automatically, and immediately visible in
the Tower.

This is explicitly *not* the same thing as controlling an already-running
agent: the launcher only ever creates new panes and never sends input to,
closes, or reconfigures anything that existed before it ran -- see
[`docs/ROADMAP.md`](docs/ROADMAP.md) for why that distinction matters and
what's still deliberately unbuilt (the "Action Layer").

Project search locations come from `~/.config/tmux-agent-tower/config.toml`:

```toml
project_roots = ["~/Projects", "~/code"]

[agents]
claude = "claude-beta"   # override the command used to launch an agent
```

Both keys are optional -- with no config at all, the launcher looks for a
handful of common directory names under your home folder (`Projects`,
`code`, `dev`, `src`, ...) that actually exist, and uses each agent's
default command name. A command is checked with `command -v` (locally, or
on the target host for a remote launch) right before spawning; if it's
missing, that one project's pane still opens as a plain shell with a
"command not found" title instead of aborting the rest.

### Optional: `Ctrl+b w` shortcut

`Ctrl+b w` stays tmux's window list until you opt in. Tower never
rewrites `~/.tmux.conf` on install. Inside Tower, `C` → **tmux shortcut**
→ **Use smart Ctrl+b w** (or `tower keys install`) adds one marked block:

* Tower is running in this session → `Ctrl+b w` jumps to that pane
  (`tower --focus`, pane id, not a window name).
* Tower is not running, or the registration is stale → the `w` command
  recorded at install time. On a stock tmux that is the window list
  (`choose-tree`). A custom `w` binding you already had is kept instead
  of being replaced with the default.

Quitting Tower does not unbind the key. The same binding checks again
the next time you press it. `tower keys restore` removes only that block
and puts the recorded `w` command back. `tower --focus` still only
navigates; it never starts a new TUI.

While Tower is open, `V` switches the list to a tmux location tree
(session, window, pane id). The project/agent list stays the default.

Enter stays inside Tower and opens Pane Control for that pane (a window
row opens its active pane's control view). The live area is the same
recent `capture-pane` text the phone uses. `P` sends one prompt, `E`
edits the display name, `Y` shows a result, `G` is the only action that
actually moves to the pane, and `X` asks before `kill-pane` of that
pane id. Cancel is the default. Tower's own pane cannot be closed from
there. `Ctrl+b w` still comes back. When the bottom widget is an
approval Codex or Claude Code names, `A` sends that documented yes key
and `N` sends the documented no key. Any other agent shows that the
approval has to be done in the real pane. Tower does not guess `y` or
Enter.

### `tower --doctor`

Runs a quick environment check (tmux/Python/curses availability, whether
you're inside tmux, configured remote hosts) and prints PASS/WARN/FAIL per
check.

### A second host

Add a line to `~/.config/tmux-agent-tower/remote-hosts.txt`, referencing an
SSH alias you've already set up in `~/.ssh/config`:

```
asus:ASUS
```

The Tower will then show that host's panes too (title/command-based
status only -- see limitations below), refreshed less frequently than the
local host, and marked `UNKNOWN`/offline gracefully if it's unreachable.
Nothing is installed on the remote host. The Workspace Launcher (`N`/`W`)
can also target this host: it runs one bounded, read-only `find` over SSH
to discover git repositories there too, with the same manual-path (`B`)
fallback if that turns up nothing.

### From your phone (experimental, this branch only)

Inside Tower, press `M` and choose **Start phone remote**. Tower prints
an `https://…ts.net` address and a pairing code on screen. Save that
address on your phone (home screen or bookmark). The header shows
`Remote: ● connected` while it is actually up.

Quitting Tower with `Q` does not stop the phone remote. Stop it from
`M` → **Stop remote**. `M` → **Autostart** can start it for you the next
time you run `tower`.

The same service is available from a shell for debugging
(`tower serve`, `tower serve --lan`, `tower serve --tailscale`). You
don't need those for normal use. Read `docs/REMOTE.md` first -- it is
the security model and the list of what this deliberately does not do.

### Language

The TUI asks once, on its very first run, whether to show Korean or
English, and remembers the answer in
`~/.config/tmux-agent-tower/config.toml` (`language = "ko"` / `"en"`).
There's no in-app way to change it again yet in this release -- edit that
line by hand and restart `tower` if you want to switch.

### Task awareness settings

Also in `~/.config/tmux-agent-tower/config.toml`, all optional and all
defaulting to the quiet/conservative choice:

```toml
show_activity = true          # default: true
show_status_duration = true   # default: true
notifications = false         # default: false -- opt in explicitly

[notifications]
waiting = true   # default: true (once notifications = true)
dead = false     # default: false
```

`notifications = true` alone is not enough to get every kind of alert --
each transition kind (`waiting`, `dead`) is independently toggleable, and
a WORKING → IDLE transition is deliberately not offered as a notification
kind at all: Tower can tell a pane stopped changing, not that a task is
actually "done."

## Supported agents

Codex, Claude Code, OpenCode, Grok CLI, Cursor Agent CLI, and a generic
shell/SSH fallback for anything else. Each adapter owns its own
execution and attention detection. There is no shared regex that marks
every agent as waiting. Codex and Claude Code numbered approval menus
are the only ones Tower will answer with a key (`1` / `3`). OpenCode
and Cursor can be recognized as approval from fixtures, but Tower does
not send a key for them because those widgets were not verified live.
See [`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md). Detection is
inherently best-effort and will drift as these CLIs' UIs change; PRs
updating a pattern (with a sanitized fixture) are welcome.

## Status meanings

* `● WORKING` -- actively producing output or showing an agent-specific
  "I'm working" signal.
* `○ IDLE` -- alive, with no agent-specific working signal. An approval
  or a question can still be set on the attention axis at the same time.
* `! 승인 필요` / `? 입력 필요` -- attention, not a status. Approval is a
  permission widget. Input is a free-text or choice question. They are
  never the same mark.
* `? UNKNOWN` -- not enough evidence either way. This is a deliberate,
  honest fallback -- see `docs/STATUS_ENGINE.md` for why "unknown" beats a
  confident wrong answer.
* `× DEAD` -- the pane or its process has exited.

Visited state is completely independent of status -- it only tracks
whether you've opened that pane from the Tower before, shown as a small
`NEW` marker in front of a row you haven't (nothing extra once you have).

## Limitations

* Status detection is pattern-based against each CLI's *current* terminal
  UI. It will drift out of date as those tools change; PRs updating a
  pattern (with a sanitized fixture) are welcome.
* The remote/multi-host view only sees pane titles, not full content (a
  deliberate simplification to avoid installing anything remotely -- see
  `docs/ARCHITECTURE.md`), so remote status is coarser than local.
* An `E` override (project/agent/title) is keyed by tmux's pane id, which
  is reused after a tmux server restart; in rare cases an override can "stick" to an
  unrelated later pane.
* Linux/WSL + tmux is the tested target. macOS should mostly work (same
  Python + tmux + curses stack) but hasn't been verified here.

## Uninstall

```bash
./scripts/uninstall.sh
```

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md). Sending input to agents,
interrupting/restarting them, or auto-approving prompts are explicitly
**not** implemented and not planned without a separate, deliberate design
pass -- this tool only ever reads and lets you navigate.

## License

MIT -- see [`LICENSE`](LICENSE).
