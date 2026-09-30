# Tower Remote (experimental, `feat/tower-remote`)

A small local web UI + API so you can check Tower's status -- and send an
explicit prompt into one specific pane -- from your phone's browser,
without installing a terminal app or exposing a full terminal.

**Status: MVP, not merged to `main`.** This lives on `feat/tower-remote`
until it's been dogfooded enough to trust. See "Known gaps" below before
relying on it for anything you can't afford to get wrong.

## Quick start

```bash
tower serve            # localhost only
tower serve --lan      # also reachable from your phone, same Wi-Fi
tower serve --port 9000
```

Run it from inside the same tmux session you want to monitor (same
requirement as `tower` itself). It prints a URL and a one-time, 5-minute
pairing code:

```
TMUX AGENT TOWER REMOTE

Local:  http://127.0.0.1:4312
LAN:    http://192.168.1.23:4312

Pairing code: 482193  (valid 5 minutes, one-time use)
```

Open the LAN address on your phone's browser, type in the code, done.
The phone's browser stores a token afterward (`localStorage`) so you
don't need to re-pair every time -- until you clear site data or the
token file is deleted.

## What it can do (v0 scope)

* Show host/project/agent/status/duration/current-activity for every pane
  Tower would show in its own TUI -- same underlying data, same adapters,
  same status engine. Nothing is re-implemented; `server/httpapi.py`
  drives a real, headless `ui.tower.Tower` instance directly.
* Let you pick one pane and send it a prompt you typed, via
  `tmux send-keys` -- the **only** write action Tower has, gated behind:
  explicit pane selection, explicit typed text, explicit send tap, a
  paired token, and a live re-check that the pane you're about to send
  to still exists, isn't dead, and still matches the project/agent you
  last saw (stale/wrong-pane rejection -- a pane_id can be reused after a
  tmux server restart, same caveat as the `E` edit menu's overrides).

## What it explicitly does not do

* No terminal emulation, no scrollback streaming, no raw captured
  terminal content over the API at all -- `/api/status` only ever
  returns the same small set of display fields the TUI shows.
* No remote-host (SSH multi-host) prompt sending yet -- `/api/prompt`
  rejects a `remote: true` pane with `"remote_unsupported"`. Read-only
  status for remote hosts works the same as it does in the TUI.
* No generic exec endpoint, no filesystem API, no credentials API. The
  one write action is narrowly "type this text into this already-open,
  already-selected pane" -- never "run this command," never "open this
  path."
* No auto-approval, no killing/restarting/interrupting an agent, no
  batched/automatic prompts to multiple panes, no cloud relay, no account
  system. See the permanent P5 boundary in `docs/ROADMAP.md`.

## Security model

* **Binding**: `tower serve` binds `127.0.0.1` only unless you pass
  `--lan`. It never defaults to a network-reachable bind.
* **Pairing**: every device needs a token, obtained once via a 6-digit
  code shown on the PC's own terminal (never transmitted anywhere else).
  The code is single-use and expires after 5 minutes.
* **Pairing rate limit**: at most 7 wrong guesses against one code before
  it's invalidated immediately (not just eventually, at TTL expiry). A
  wrong, expired, and locked-out code all produce the exact same
  `{"error": "invalid_code"}` response -- an attacker on your LAN can't
  tell which case they hit. Getting a new code after a lockout requires
  typing `r` + Enter in the `tower serve` terminal itself; there is no
  HTTP path to a new code at all (see `test_no_http_endpoint_can_regenerate_pairing_code`).
* **Tokens**: opaque random strings, persisted at
  `~/.config/tmux-agent-tower/remote-tokens.json` (gitignored, never
  committed -- same boundary as every other local state file in this
  project), written with owner-only `0600` permissions (and its parent
  directory `0700`) -- an existing, more permissive file is tightened the
  next time `tower serve` starts. Best-effort on non-POSIX filesystems
  (Windows): a failed `chmod` there is swallowed, never a crash.
* **Host header check**: every request's `Host` header must match an
  address this process actually printed to you (`localhost`, `127.0.0.1`,
  or the detected LAN IP for `--lan`) -- defense against DNS rebinding
  from a malicious page open in another tab on the same network. This is
  a narrow mitigation, not a substitute for TLS -- see below.
* **Prompt send**: literal `tmux send-keys -l` (never interpreted as a
  key name), a hard length cap (4000 chars), a request body size cap
  (8KB), and the stale/wrong-pane re-check described above. No shell is
  ever invoked with the prompt text as an argument -- it can't escape
  into a host command regardless of its content.

## No TLS -- read this before using `--lan`

`tower serve --lan` is **plain HTTP, unencrypted, no certificate, no
TLS**. That is a real limitation, not a detail:

* The pairing code, the bearer token, and every prompt you send travel
  as plaintext on your local network. Anyone else who can observe that
  network traffic (a compromised device on the same Wi-Fi, a malicious
  access point) can read them.
* **Use this only on a private network you trust** -- your own home
  Wi-Fi, not a coffee shop, airport, hotel, or any shared/public Wi-Fi.
* **Never port-forward this to the public internet.** There is no
  authentication model here strong enough for that exposure, and this
  project will not add one for plain HTTP -- see the Tailscale note
  below for the intended path to off-LAN access instead.
* The Host-header check above stops one specific attack (DNS rebinding
  against the LAN server) -- it is **not** encryption and does not make
  this safe on an untrusted network. Don't read it as "safe enough for
  public Wi-Fi because of the Host check"; it isn't.
* Off-LAN access is planned via Tailscale (see `docs/ROADMAP.md`'s Tower
  Remote section) once the LAN MVP itself has been dogfooded enough to
  trust -- not via opening this port to the internet.

## Known gaps (v0 MVP)

* **No QR code image.** Generating one correctly needs either a new
  dependency or a from-scratch encoder -- both felt like more risk than
  this MVP needed. You type the pairing code by hand; the URL can be
  typed too, or bookmarked once. Deferred, not forgotten.
* **No mDNS/`tower.local` auto-discovery.** You read the printed LAN IP
  off the PC's terminal. Fine for a first real phone connection; a nice
  quality-of-life addition later.
* **Pairing-code regeneration** is available (type `r` + Enter in the
  `tower serve` terminal) but requires local terminal access -- there is
  deliberately no remote/HTTP way to do it (see the security model
  above).
* **Single tmux session only** -- exactly the same scope `tower` itself
  has today (the session the process is run from), not a cross-session
  view.
* **A plain shell pane's "prompt" is shell input.** If the pane you pick
  happens to be a bare shell rather than a coding agent, sending a
  prompt to it is exactly as consequential as typing that text yourself
  in that terminal. This is not a bug -- Tower Remote's write action is
  scoped to *panes*, not to *agents specifically* -- but it's worth
  knowing before you tap Send on a shell pane's card.
* **Tailscale/off-LAN access**: not built or tested yet. `--lan` is LAN
  only for now; a Tailscale-based path is the planned next step once the
  LAN MVP itself has been dogfooded enough to trust (see
  `docs/ROADMAP.md`).
