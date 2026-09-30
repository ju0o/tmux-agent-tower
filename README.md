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
AGENT CONTROL TOWER
● WORKING 3  ! WAITING 1  ○ IDLE 2  ? UNKNOWN 0  × DEAD 0

▼ MAINPC  ●2 !1 ○1 ?0 ×0
  alpha-api            Codex       ● WORKING    SEEN
> web-app              Codex       ! WAITING    NEW
  docs                 Grok        ○ IDLE       SEEN

▼ ASUS  ●1 !0 ○1 ?0 ×0
  billing-service      Claude      ● WORKING    NEW
  internal-notes       OpenCode    ○ IDLE       SEEN

↑↓ Move   Enter Open   E Rename   N Add project   W New workspace   R Refresh   Q/Ctrl+C Exit
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

* One screen, grouped by host, showing project / agent / status / visit
  for every tmux pane.
* Status and "have I looked at this yet" are tracked completely
  separately -- an agent working away in a pane you haven't opened yet
  correctly shows `WORKING`, not some vague "checking" placeholder.
* Project name is auto-discovered from the pane's git repository (falls
  back to the directory name), with a manual override (`E`) that survives
  future refreshes.
* Works across a second host over SSH, degrading gracefully to
  `UNKNOWN`/offline if that host isn't reachable -- the local TUI never
  blocks waiting on it.

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

Inside any tmux session:

```
tower
```

This creates (or jumps to) a `CONTROL` window running the TUI -- **not** in
the pane you typed it from. There's one Tower per session, and `tower`
always takes you to that same place, the same way `Ctrl+b` then `w` does
from anywhere else in tmux (opt-in, see below). If you specifically want
the TUI running in the current pane instead, use `tower --here`.

| Key | Action |
|---|---|
| `↑` / `↓` (or `j`/`k`) | Move selection |
| `Enter` | Jump to the selected pane |
| `E` | Set a custom project name for the selected pane |
| `N` | Add one project (Workspace Launcher, single) |
| `W` | Start a new workspace (Workspace Launcher, multi) |
| `R` | Refresh immediately |
| `Q` / `Ctrl+C` | Quit |

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

Binding `Ctrl+b w` to jump straight to the Tower is convenient, but it
**replaces tmux's built-in `choose-tree` window picker** on that key. The
installer does not do this automatically. If you want it, add this to your
`~/.tmux.conf` yourself:

```tmux
unbind-key w
bind-key w run-shell -b "tower"
```

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

### Language

The TUI asks once, on its very first run, whether to show Korean or
English, and remembers the answer in
`~/.config/tmux-agent-tower/config.toml` (`language = "ko"` / `"en"`).
There's no in-app way to change it again yet in this release -- edit that
line by hand and restart `tower` if you want to switch.

## Supported agents

Codex, Claude Code, OpenCode, Grok CLI, Cursor Agent CLI, and a generic
shell/SSH fallback for anything else. Every adapter's WORKING/IDLE (and,
except OpenCode, WAITING) patterns were verified against a real, live
session of that CLI -- see [`docs/STATUS_ENGINE.md`](docs/STATUS_ENGINE.md)
for exactly what evidence each one looks for. OpenCode's own
permission/approval prompt specifically was never observed live (the
tested session auto-approved writes), so WAITING falls back to a generic
pattern for that agent only -- see `adapters/opencode.py`'s docstring.
Detection is inherently best-effort and will drift as these CLIs' UIs
change; PRs updating a pattern (with a sanitized fixture) are welcome.

## Status meanings

* `● WORKING` -- actively producing output or showing an agent-specific
  "I'm working" signal.
* `! WAITING` -- looks like it's waiting on your approval/input.
* `○ IDLE` -- alive, nothing pending.
* `? UNKNOWN` -- not enough evidence either way. This is a deliberate,
  honest fallback -- see `docs/STATUS_ENGINE.md` for why "unknown" beats a
  confident wrong answer.
* `× DEAD` -- the pane or its process has exited.

`VISIT` (`NEW`/`SEEN`) is completely independent of status -- it only
tracks whether you've opened that pane from the Tower before.

## Limitations

* Status detection is pattern-based against each CLI's *current* terminal
  UI. It will drift out of date as those tools change; PRs updating a
  pattern (with a sanitized fixture) are welcome.
* The remote/multi-host view only sees pane titles, not full content (a
  deliberate simplification to avoid installing anything remotely -- see
  `docs/ARCHITECTURE.md`), so remote status is coarser than local.
* A custom title (`E`) is keyed by tmux's pane id, which is reused after a
  tmux server restart; in rare cases an override can "stick" to an
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
