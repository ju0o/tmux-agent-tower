#!/usr/bin/env bash
# Reverses what install.sh did. Safe to run even if install.sh was never
# run (each step is a no-op if there's nothing to undo).

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER_BEGIN="# >>> tmux-agent-tower >>>"
MARKER_END="# <<< tmux-agent-tower <<<"
BASHRC="$HOME/.bashrc"

echo "Tmux Agent Tower uninstaller"
echo

# --- 1. Remove the marked block from ~/.bashrc, if present -----------------

if [[ -f "$BASHRC" ]] && grep -qF "$MARKER_BEGIN" "$BASHRC"; then
    BACKUP="$BASHRC.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$BASHRC" "$BACKUP"
    echo "[OK] backed up $BASHRC -> $BACKUP"

    awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
        $0 == b {skip=1; next}
        $0 == e {skip=0; next}
        skip {next}
        {print}
    ' "$BASHRC" > "$BASHRC.tmp" && mv "$BASHRC.tmp" "$BASHRC"

    echo "[OK] removed tmux-agent-tower block from $BASHRC"
else
    echo "[OK] no tmux-agent-tower block found in $BASHRC -- nothing to remove."
fi

# --- 2. Uninstall the package ------------------------------------------------

python3 -m pip uninstall -y tmux-agent-tower >/tmp/tower-uninstall.log 2>&1 \
    && echo "[OK] uninstalled via pip" \
    || echo "[INFO] pip uninstall reported nothing to remove (see /tmp/tower-uninstall.log)"

if [[ -d "$REPO_DIR/.venv" ]]; then
    echo "[INFO] leaving $REPO_DIR/.venv in place -- delete it yourself if you"
    echo "       installed via the venv fallback and want it gone:"
    echo "       rm -rf '$REPO_DIR/.venv'"
fi

echo
echo "[INFO] left untouched on purpose: ~/.cache/tmux-agent-tower (visit history)"
echo "       and ~/.config/tmux-agent-tower (your host/remote config). Remove"
echo "       those yourself if you want a completely clean slate:"
echo "       rm -rf ~/.cache/tmux-agent-tower ~/.config/tmux-agent-tower"
echo
echo "Done."
