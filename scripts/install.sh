#!/usr/bin/env bash
# Idempotent installer for Tmux Agent Tower.
#
# - Installs the `tower` console script for the current user.
# - Adds a PATH line to ~/.bashrc *only if missing*, after backing it up.
# - Does NOT touch ~/.tmux.conf or rebind any tmux key -- see README.md's
#   "Optional: Ctrl+b w shortcut" section for that, by design (silently
#   overriding tmux's built-in `choose-tree` binding for every OSS user
#   would be a rude default).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER_BEGIN="# >>> tmux-agent-tower >>>"
MARKER_END="# <<< tmux-agent-tower <<<"

echo "Tmux Agent Tower installer"
echo "Repository: $REPO_DIR"
echo

# --- 1. Install the package for the current user --------------------------

install_with_pip() {
    python3 -m pip install --user -e "$REPO_DIR" >/tmp/tower-install.log 2>&1
}

VENV_DIR=""

if install_with_pip; then
    echo "[OK] installed with 'pip install --user -e .'"
    USER_BASE="$(python3 -m site --user-base)"
    BIN_DIR="$USER_BASE/bin"
else
    echo "[INFO] 'pip install --user' failed (often PEP 668 'externally managed'"
    echo "       environment) -- falling back to a dedicated virtualenv."
    VENV_DIR="$REPO_DIR/.venv"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR"
    BIN_DIR="$VENV_DIR/bin"
    echo "[OK] installed into $VENV_DIR"
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    NEED_PATH_LINE=1
else
    NEED_PATH_LINE=0
fi

# --- 2. Wire up ~/.bashrc, idempotently ------------------------------------

BASHRC="$HOME/.bashrc"

if [[ "$NEED_PATH_LINE" -eq 1 ]]; then
    if [[ -f "$BASHRC" ]] && grep -qF "$MARKER_BEGIN" "$BASHRC"; then
        echo "[OK] ~/.bashrc already wired up (marker found) -- leaving it alone."
    else
        if [[ -f "$BASHRC" ]]; then
            BACKUP="$BASHRC.bak.$(date +%Y%m%d-%H%M%S)"
            cp "$BASHRC" "$BACKUP"
            echo "[OK] backed up $BASHRC -> $BACKUP"
        fi

        {
            echo ""
            echo "$MARKER_BEGIN"
            echo "export PATH=\"$BIN_DIR:\$PATH\""
            echo "$MARKER_END"
        } >>"$BASHRC"

        echo "[OK] appended PATH line to $BASHRC"
    fi
else
    echo "[OK] $BIN_DIR already on PATH -- no ~/.bashrc change needed."
fi

# --- 3. Sanity check --------------------------------------------------------

echo
if "$BIN_DIR/tower" --version >/dev/null 2>&1; then
    echo "[OK] 'tower' runs: $("$BIN_DIR/tower" --version)"
else
    echo "[WARN] could not run '$BIN_DIR/tower' -- check /tmp/tower-install.log"
fi

echo
echo "Done. Open a new shell (or 'source ~/.bashrc'), then run 'tower' inside tmux."
echo "See README.md for the optional 'Ctrl+b w' shortcut and remote-host setup."
