# Security Policy

Tmux Agent Tower is a local-first, read-only monitoring tool. It does not
run a server, does not phone home, and does not require any account or API
key of its own.

## What it touches

* Runs `tmux list-panes` / `tmux capture-pane` on your local tmux server
  (read-only).
* Optionally runs the same two *read-only* commands over an **existing**
  SSH alias you configure yourself, to show a second host's panes.
* Writes only to `~/.cache/tmux-agent-tower/` (visit state) and
  `~/.config/tmux-agent-tower/` (your own config: host label, remote host
  list). It never writes into a monitored pane and never renames your
  actual tmux pane titles.

It never sends keystrokes into a monitored pane, never kills or restarts a
process, and never stores or transmits captured pane content anywhere
outside your own machine.

## Reporting a vulnerability

Please open a private report via GitHub's "Report a vulnerability" button
on this repository's Security tab, rather than a public issue, if you
believe you've found a security-relevant bug (e.g. a way for a malicious
pane title/content to cause command injection). Include steps to reproduce
if possible.
