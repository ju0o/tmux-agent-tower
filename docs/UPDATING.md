# Updating Tower

Tower does not update itself or contact GitHub during normal startup. Network
access happens only when you run an update command.

```sh
tower --check-update
tower --update --dry-run
tower --update
```

`--check-update` reports the running package version and source, the `tower` on
`PATH`, and the latest published official GitHub Release. If GitHub cannot be
reached, it reports `OFFLINE`; Tower continues to work. Invalid or ambiguous
release metadata is rejected.

The default channel follows the installed package version: versions ending in
`rcN` check pre-releases, and stable-shaped versions check stable releases.
GitHub may mark a stable-shaped tag as a pre-release; that metadata does not
silently move a stable install onto the RC track. Choose `--channel rc` to opt in.
RC installs stay on the RC track; use `--channel stable` to move to stable. The
selected tag and commit must be published and verifiable before an update can
proceed.

`--update --dry-run` prints the planned paths and makes no changes. Applying an
update requires an interactive `yes` confirmation. Automatic updates are
supported only for a clean editable user install, the installer `.venv`
fallback, or a Tower-managed install. Unmanaged packages, custom launchers, and
dirty developer checkouts are left alone with a manual update explanation.
Launcher paths must not pass through symlinks or directories writable by other
users (except sticky shared directories such as `/tmp`); unsafe paths are
refused.

For a supported install, Tower downloads the exact verified GitHub tag source,
installs it into a versioned directory under the user's data directory, checks
the package version and import path, backs up the current launcher, then
atomically switches that launcher. A failed verification restores the prior
launcher. Configuration, cache, tmux sessions and panes, and running Tower
processes are not changed. Restart Tower yourself to use the new version.
Previous version directories and launcher backups are retained for rollback and
are not automatically pruned.

If multiple supported launchers reuse one verified version directory, each
launcher receives its own private path-bound registration. A launcher is not
treated as managed merely because another launcher uses the same package copy.

Ctrl-C during launcher activation restores the previous launcher. A forced
process kill or power loss cannot run rollback; the new launcher is checked
before the atomic switch, and the previous launcher backup remains available.

## One-time migration from an older Tower

Older releases do not contain the updater. Get the `scripts/migrate-update.sh`
script from a fresh, clean clone at an exact published Release tag that includes
the updater. Do not run it from a development checkout. The script verifies
that the checkout is clean and exactly at a tag; Tower then confirms that the
tag and commit are an official published Release before it plans a launcher
switch.

```sh
PUBLISHED_TAG=vX.Y.Z  # Replace with an exact official Release tag that includes the updater.
git clone --branch "$PUBLISHED_TAG" --depth 1 https://github.com/ju0o/tmux-agent-tower.git tower-update-bootstrap
cd tower-update-bootstrap
./scripts/migrate-update.sh --dry-run
./scripts/migrate-update.sh
```

If the existing installation is stable and the bootstrap tag is an RC, opt in
explicitly with `--channel rc`. A migration never downgrades the existing
`PATH` installation. If the old package is unmanaged or cannot be inspected,
use the normal install instructions for the chosen official Release instead;
the updater will not guess how to replace it.

The TUI does not check for updates or show update notifications. This keeps
startup network-free and avoids interrupting active work.
