# Tower Remote (experimental, `feat/tower-remote`)

A small local web UI + API so you can check Tower's status -- and send an
explicit prompt into one specific pane -- from your phone's browser,
without installing a terminal app or exposing a full terminal.

**Status: MVP, not merged to `main`.** This lives on `feat/tower-remote`
until it's been dogfooded enough to trust. See "Known gaps" below before
relying on it for anything you can't afford to get wrong.

## Quick start

From the Tower TUI (this is the normal way -- no shell command):

```
tower
```

then `M` → **휴대폰 원격 시작** / **Start phone remote**. The screen
shows the address and a pairing code. `Q` leaves the remote running;
`M` → **원격 종료** / **Stop remote** turns it off. The header badge
(`원격: ● 연결됨` / `Remote: ● connected`) is a live health check, not
a remembered flag.

The same server can still be started from a shell. That path is for
debugging and automation, not for day-to-day use:

```bash
tower serve              # localhost only
tower serve --lan        # also reachable from your phone, same Wi-Fi (plain HTTP)
tower serve --tailscale  # reachable from your phone anywhere, over your tailnet (HTTPS)
tower serve --port 9000
```

`--lan` and `--tailscale` are mutually exclusive. If you have Tailscale,
prefer the TUI's phone-remote action (or `--tailscale`) -- see the
dedicated section below for why. A remote started either way is the
same process: the TUI will see it, and will not start a second one.

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
  `tmux send-keys`. A card opens the detail screen; it does not send.
  The send button is a separate tap, and it re-checks that the pane
  still exists, isn't dead, and still matches the project/agent you
  last saw (stale/wrong-pane rejection -- a pane_id can be reused after a
  tmux server restart, same caveat as the `E` edit menu's overrides).
* Open a pane detail from a card: project, agent, status, duration,
  current activity, pane name, host, and a **LIVE PANE** box. The box is
  plain text from `tmux capture-pane` of the visible recent screen
  (`GET /api/panes/<key>/screen`), not a terminal emulator and not the
  scrollback history. The phone polls about once a second, backs off
  toward two seconds when a round trip is slow, and pauses while the
  page is hidden. There is no WebSocket.
* Edit project, agent, or pane title from that same detail screen, or
  reset them to auto-detection. Those writes go through the same
  `OverrideStore` the TUI uses. The TUI reloads the file when it
  changes, so the next refresh shows the phone's values. A local pane
  title is also pushed with `tmux select-pane -T`.

## What it explicitly does not do

* No terminal emulator and no scrollback stream. `/api/status` still
  returns only the small display fields the TUI shows. Recent screen
  text exists only on `GET /api/panes/<key>/screen`, and only for a
  local pane in the bound session.
* No remote-host (SSH) live screen and no remote prompt. A key that
  contains `:` is `remote_unsupported`. The phone says, in plain text,
  that an SSH host pane shows title and status only. It does not invent
  a screen. `/api/prompt` rejects `remote: true` the same way. Read-only
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
  `--lan`. It never defaults to a network-reachable bind. `--tailscale`
  also keeps the backend on `127.0.0.1` -- it is never `0.0.0.0` in that
  mode; reachability comes from a Tailscale Serve mapping, not from a
  wider bind (see `tests/test_main_serve_tailscale.py`).
* **Pairing**: every device needs a token, obtained once via a 6-digit
  code shown on the PC's own terminal (never transmitted anywhere else).
  The code is single-use and expires after 5 minutes.
* **Pairing rate limit**: at most 7 wrong guesses against one code before
  it's invalidated immediately (not just eventually, at TTL expiry). A
  wrong, expired, and locked-out code all produce the exact same
  `{"error": "invalid_code"}` response -- an attacker on your LAN can't
  tell which case they hit. Getting a new code after a lockout requires
  choosing **New pairing code** in the Tower `M` menu, or typing `r` +
  Enter in a foreground `tower serve` terminal; there is no HTTP path to
  a new code at all (see `test_no_http_endpoint_can_regenerate_pairing_code`).
* **Tokens**: opaque random strings, persisted at
  `~/.config/tmux-agent-tower/remote-tokens.json` (gitignored, never
  committed -- same boundary as every other local state file in this
  project), written with owner-only `0600` permissions (and its parent
  directory `0700`) -- an existing, more permissive file is tightened the
  next time `tower serve` starts. Best-effort on non-POSIX filesystems
  (Windows): a failed `chmod` there is swallowed, never a crash.
* **Host header check**: every request's `Host` header must match an
  address this process actually printed to you (`localhost`, `127.0.0.1`,
  the detected LAN IP for `--lan`, or -- for `--tailscale` -- this
  machine's own MagicDNS name and Tailscale IPv4, read from
  `tailscale status --json` at startup, never a wildcard) -- defense
  against DNS rebinding from a malicious page open in another tab on the
  same network. This is a narrow mitigation, not a substitute for TLS --
  see below.
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
* Off-LAN access is via Tailscale (`tower serve --tailscale`, next
  section) -- not via opening this port to the internet.

## Tailscale mode -- `tower serve --tailscale`

The intended path for using Tower Remote from anywhere (LTE/5G, another
network) without ever exposing the port to the LAN or the internet. Your
phone needs only the Tailscale app + a browser, logged in to the same
tailnet. No SSH app, no terminal emulator, no shared Wi-Fi.

### Architecture (WSL2 on Windows)

```
Phone (Tailscale app + browser)
  --tailnet, WireGuard--> Windows host Tailscale
                            Tailscale Serve: https://<machine>.<tailnet>.ts.net  (tailnet only)
                              --> Windows 127.0.0.1:4312
                                    (WSL2 localhost forwarding, built in)
                                    --> WSL Tower Remote backend, bound to 127.0.0.1:4312
```

* Tower shells out to the **Windows** `tailscale.exe` (found at the
  standard `/mnt/c/Program Files/Tailscale/tailscale.exe` interop path,
  falling back to a `tailscale` on `PATH` for native Linux/macOS). It
  never installs or starts a Tailscale daemon inside WSL -- Tailscale's
  own guidance is not to run both a Windows and a WSL-native instance.
* The backend still binds `127.0.0.1` only. `tailscale serve` is what
  makes it reachable, and Serve is tailnet-only by construction.
* HTTPS terminates at Windows Tailscale with a Let's Encrypt certificate
  for your MagicDNS name; the last hop (Windows loopback -> WSL loopback)
  never leaves the machine.

### What `--tailscale` does, in order

1. Starts the backend on `127.0.0.1:<port>`.
2. Finds `tailscale.exe`. If missing: prints how to install/log in, exits 1.
3. Verifies from the **Windows** side (`curl.exe http://127.0.0.1:<port>/api/health`)
   that Windows can actually reach the WSL backend -- the exact hop Serve
   will use. If that fails (or can't be verified because `curl.exe` is
   missing), it prints `Windows localhost에서 Tower Remote에 연결할 수
   없습니다.` and **does not** touch Serve config. Fail closed, never
   "probably works."
4. Reads `tailscale status --json` for this machine's MagicDNS name. No
   MagicDNS = no hostname for Serve's HTTPS certificate = clear error and
   exit (see "Known gaps").
5. Checks `tailscale serve status --json` for what's already at `:443`:
   * nothing -> runs `tailscale serve --bg <port>` (the current CLI's form
     for "proxy `https://<name>/` to `http://127.0.0.1:<port>`").
   * already pointing at our own backend (e.g. previous run didn't clean
     up) -> reused as-is, nothing re-issued.
   * pointing **anywhere else** -> refuses, prints what is there and the
     exact `tailscale serve --https=443 off` you'd run if you decide it's
     no longer needed. Tower never overwrites another mapping.
6. Adds the MagicDNS name and Tailscale IPv4 to the `Host` allowlist,
   prints the `https://` URL and the pairing code.
7. On Ctrl+C: re-reads Serve status and removes the `:443` mapping
   **only if it still points at our own backend** (via the per-port
   `tailscale serve --https=443 off`). If it changed underneath us, it's
   left alone and you're told the exact command. `tailscale serve reset`
   is never called, by anything, ever (`tests/test_server_tailscale.py`).

`tailscale funnel` is never invoked either -- Funnel is public-internet
exposure, and this project's auth model is not designed for that.

### What it looks like

```
TMUX AGENT TOWER REMOTE

Tailscale: ONLINE
https://<machine>.<tailnet>.ts.net/

Pairing code: 424429  (valid 5 minutes, one-time use)
Open the address above on your phone's browser and enter this code.

Type 'r' + Enter any time for a new pairing code. Ctrl+C to stop.
```

Pairing, bearer tokens, rate limiting, `Host` validation, the prompt
length/body caps, and the stale-pane re-check all apply exactly as in
LAN mode. Being on the tailnet is a *second* layer, not a replacement:
Tailscale identity + WireGuard/HTTPS transport + Tower pairing/token.

### Autostart

`M` → **자동 시작 설정** / **Autostart** writes `[remote] autostart` in
`~/.config/tmux-agent-tower/config.toml` (default `false`). When it is
`true`, `tower` starts the TUI and the Tailscale remote together. A
failed start does not block the TUI; the header shows
`원격: ! 오류` / `Remote: ! error`.

That is "when I open Tower", not "when Windows boots". A login-time
service (`tower remote install-service`, WSL + Tailscale already up) is
still a later slice. The MagicDNS URL stays stable, so a phone bookmark
keeps working across starts.

## Known gaps (v0 MVP)

* **MagicDNS + HTTPS certificates must be enabled on your tailnet** for
  `--tailscale`. Serve's HTTPS mode needs a hostname to issue a
  certificate for; without MagicDNS Tower prints `no_magicdns` and stops
  rather than fall back to something half-working. A Tailscale-IP-only
  path (`http://100.x.y.z:<port>` over the tailnet) is *not* implemented
  -- it would need a non-localhost bind and gives up HTTPS, so it's
  documented here as a possible future fallback, not silently done.
* **`--tailscale` assumes the Windows-host + WSL2 layout** described
  above. On native Linux/macOS with a local `tailscale` on `PATH` the same
  `serve --bg` flow should work in principle, but the Windows-side
  `curl.exe` reachability check is skipped there (`None` -> treated as
  "could not verify" -> refuses to proceed). Untested; treat as
  unsupported until it has been dogfooded.
* **Serve mapping is always at `:443`.** If you already serve something
  else at `https://<machine>.<tailnet>.ts.net/`, Tower refuses (see
  above) instead of picking another port. A `--https-port` option is a
  reasonable follow-up.
* **Phone acceptance of LIVE PANE, identity edit, and prompt send is
  still a manual step.** The PC can exercise the same API; a real phone
  on the tailnet is the acceptance that has to be done by the person
  holding it.

* **No QR code image.** Generating one correctly needs either a new
  dependency or a from-scratch encoder -- both felt like more risk than
  this MVP needed. You type the pairing code by hand; the URL can be
  typed too, or bookmarked once. Deferred, not forgotten.
* **No mDNS/`tower.local` auto-discovery.** You read the printed LAN IP
  off the PC's terminal. Fine for a first real phone connection; a nice
  quality-of-life addition later. (With `--tailscale` the URL is your
  stable MagicDNS name, so this matters much less there.)
* **Pairing-code regeneration** is local only: the `M` menu, or `r` +
  Enter in a foreground `tower serve` terminal. There is deliberately no
  remote/HTTP way to do it (see the security model above).
* **Single tmux session only** -- exactly the same scope `tower` itself
  has today, not a cross-session view. The session is fixed when the
  remote starts: the Tower (or `tower serve` process) that starts it
  passes its own session explicitly, and the detached server never
  re-infers one from its environment. The `M` menu shows `관제 세션:`
  so you can see which session the remote is bound to. If that session
  is deleted, the header goes to `원격: ! 오류` (`source_session_missing`),
  `/api/status` returns `{"ok": false, "error": "source_session_missing"}`
  instead of an empty local list, and the phone page says the watched
  session has ended. Starting from a Tower in a *different* session asks
  whether to switch; it never switches by itself.
* **A plain shell pane's "prompt" is shell input.** If the pane you pick
  happens to be a bare shell rather than a coding agent, sending a
  prompt to it is exactly as consequential as typing that text yourself
  in that terminal. This is not a bug -- Tower Remote's write action is
  scoped to *panes*, not to *agents specifically* -- but it's worth
  knowing before you tap Send on a shell pane's card.