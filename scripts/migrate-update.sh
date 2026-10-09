#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATUS="$(git -C "$ROOT" status --porcelain --untracked-files=normal)" || {
    echo "Cannot inspect the migration checkout." >&2
    exit 2
}
if [[ -n "$STATUS" ]]; then
    echo "Refusing migration from a dirty checkout; use a fresh official Release clone." >&2
    exit 2
fi

if ! git -C "$ROOT" describe --tags --exact-match HEAD >/dev/null 2>&1; then
    echo "Refusing migration unless this checkout is at an exact Release tag." >&2
    exit 2
fi

ARGS=()
while (($#)); do
    case "$1" in
        --dry-run)
            ARGS+=("$1")
            shift
            ;;
        --channel)
            if (($# < 2)) || [[ ! "$2" =~ ^(auto|stable|rc)$ ]]; then
                echo "Usage: $0 [--dry-run] [--channel auto|stable|rc]" >&2
                exit 2
            fi
            ARGS+=("$1" "$2")
            shift 2
            ;;
        --channel=auto|--channel=stable|--channel=rc)
            ARGS+=("$1")
            shift
            ;;
        *)
            echo "Usage: $0 [--dry-run] [--channel auto|stable|rc]" >&2
            exit 2
            ;;
    esac
done

exec "$ROOT/scripts/dev-python" -m tmux_agent_tower.main --migrate-update "${ARGS[@]}"
