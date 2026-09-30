#!/usr/bin/env bash
# Thin wrapper: `tower --doctor` does the real work. This just makes sure
# the check is reachable even before `tower` is on PATH (e.g. right after
# cloning, before running install.sh).
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v tower >/dev/null 2>&1; then
    exec tower --doctor
fi

if [[ -x "$REPO_DIR/.venv/bin/tower" ]]; then
    exec "$REPO_DIR/.venv/bin/tower" --doctor
fi

PYTHONPATH="$REPO_DIR/src" exec python3 -m tmux_agent_tower.main --doctor
