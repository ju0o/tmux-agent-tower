# Contributing

Thanks for considering a contribution! This project is a small, local-first
tool, so the bar for "does this need a new abstraction" is high.

## Setup

```bash
git clone https://github.com/ju0o/tmux-agent-tower
cd tmux-agent-tower
python3 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest
```

Workspace dogfood builds a temporary three-level git tree under the test
tmp directory. It does not read a home directory or a fixed Projects
folder. Remote browser tests use synthetic paths under
`/remote/agent-host` and do not open SSH.

The optional remote tree dogfood is separate. It runs only when an SSH
test target is explicitly configured through `TOWER_REMOTE_TREE`; unset
means skip.

## Adding or improving an agent adapter

Adapters live in `src/tmux_agent_tower/adapters/`. Each one is small and
self-contained on purpose (see `docs/STATUS_ENGINE.md`). If you're adding
support for a new coding agent CLI, or fixing false positives/negatives for
an existing one:

1. Capture a **sanitized** sample of the real pane output for the state
   you're targeting (remove any project names, paths, keys, or personal
   text) and add it under `fixtures/<agent>-<state>.txt`.
2. Write a regression test in `tests/test_adapters.py` against that
   fixture.
3. Keep the adapter's `classify()` conservative: returning `None` ("no
   opinion") is always safer than guessing wrong. See the status engine's
   priority rules in `docs/STATUS_ENGINE.md`.

Please do not paste real captured terminal output (yours or anyone else's)
into an issue or PR without redacting it first — it can easily contain API
keys, tokens, or private project details.

## Pull requests

* Keep PRs focused; small logical commits are preferred over one giant
  diff.
* Run `pytest` before opening a PR.
* If you're changing status-detection behavior, explain the real-world
  pane output that motivated the change.
