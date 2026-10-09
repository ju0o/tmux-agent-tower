"""``tower`` CLI entry point."""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import subprocess
import sys

from . import __version__
from .i18n import t
from .server import service as remote_service
from .tmux import capture as tmux_capture
from .tmux import keybind
from .tmux import registration
from .ui.tower import run as run_ui, load_remote_hosts

DEFAULT_SERVE_PORT = 4312


def _print_result(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    mark = "✓" if ok else ("!" if warn else "✗")
    status_word = t("doctor.ok") if ok else detail or t("doctor.fail")
    line = f"{mark} {label:<22} {status_word}"
    print(line)
    return ok


def runtime_facts() -> dict:
    """Where this process actually imported the package from.

    ``mismatch`` is true only when a dev launcher set ``TOWER_DEV_ROOT``
    and the imported file is outside that checkout. ``tree`` is ``dev``
    for a checkout ``src`` tree and ``packaged`` for a copy under
    site-packages. The working tree on disk is what was imported.
    """

    import tmux_agent_tower

    source = str(os.path.realpath(tmux_agent_tower.__file__))
    root = os.environ.get("TOWER_DEV_ROOT", "").strip()
    mismatch = False
    if root:
        root_real = os.path.realpath(root)
        try:
            mismatch = os.path.commonpath([source, root_real]) != root_real
        except ValueError:
            mismatch = True
    return {
        "source": source,
        "python": sys.executable,
        "version": __version__,
        "mismatch": mismatch,
        "tree": tree_kind(source),
    }


def tree_kind(source: str) -> str:
    """``dev`` when the import is a checkout tree, ``packaged`` when it is installed."""

    normalized = source.replace("\\", "/")
    if "/site-packages/" in normalized and "/src/tmux_agent_tower/" not in normalized:
        return "packaged"
    return "dev"


def checkout_root(source: str) -> str:
    """Directory that contains ``src`` for a dev import, else the package dir."""

    normalized = os.path.realpath(source)
    marker = f"{os.sep}src{os.sep}tmux_agent_tower{os.sep}"
    if marker in normalized:
        return normalized.split(marker, 1)[0]
    return os.path.dirname(normalized)


def inspect_path_tower(which=None, runner=None) -> dict:
    """What plain ``tower`` on PATH imports, without starting a TUI.

    The child does not inherit ``PYTHONPATH`` or ``TOWER_DEV_ROOT``, so a
    dev shell cannot hide another checkout's editable install.
    """

    exe = shutil.which("tower") if which is None else which
    if not exe:
        return {"found": False, "source": "", "same": None}
    ours = checkout_root(runtime_facts()["source"])
    python = _shebang_python(exe)
    if not python:
        return {"found": True, "source": "", "same": None, "exe": exe}
    if runner is None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("TOWER_DEV_ROOT", None)
        code = "import os, tmux_agent_tower; print(os.path.realpath(tmux_agent_tower.__file__))"
        completed = subprocess.run(
            [python, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
        source = (completed.stdout or "").strip().splitlines()
        source = source[-1] if source and completed.returncode == 0 else ""
    else:
        source = runner(python)
    same = None
    if source:
        try:
            same = os.path.commonpath([os.path.realpath(source), ours]) == ours
        except ValueError:
            same = False
    return {"found": True, "source": source, "same": same, "exe": exe}


def _shebang_python(path: str) -> str:
    try:
        first = open(path, encoding="utf-8", errors="replace").readline().strip()
    except OSError:
        return ""
    if not first.startswith("#!"):
        return ""
    program = first[2:].strip().split()
    if not program:
        return ""
    candidate = program[0]
    if candidate.endswith("python") or candidate.endswith("python3") or "/python" in candidate:
        return candidate
    return ""


def _warn_if_foreign() -> None:
    """Stop before curses when a dev launcher imported some other checkout."""

    facts = runtime_facts()
    if not facts["mismatch"]:
        return
    print("Source mismatch: imported file is outside TOWER_DEV_ROOT", file=sys.stderr)
    print(f"Tower source: {facts['source']}", file=sys.stderr)
    raise SystemExit(1)


def run_doctor() -> int:
    print(t("doctor.title"))
    print()

    problems = 0
    facts = runtime_facts()
    print(f"Tower source: {facts['source']}")
    print(f"Python: {facts['python']}")
    print(f"Version: {facts['version']}")
    print(f"Working tree: {facts['tree'].upper() if facts['tree'] == 'dev' else facts['tree']}")
    if facts["mismatch"]:
        print("Source mismatch: imported file is outside TOWER_DEV_ROOT")
        problems += 1
    path_tower = inspect_path_tower()
    if not path_tower["found"]:
        print("PATH tower: not found")
    elif path_tower["same"] is True:
        print("PATH tower: same checkout")
    elif path_tower["same"] is False:
        print(f"PATH tower: {path_tower['source']}")
        print("PATH tower: OTHER CHECKOUT")
    else:
        print(f"PATH tower: {path_tower.get('exe') or 'unreadable'}")
    print()

    tmux_path = shutil.which("tmux")
    if not _print_result(t("doctor.check.tmux_installed"), bool(tmux_path)):
        problems += 1

    if tmux_path:
        try:
            version = subprocess.run(
                ["tmux", "-V"], capture_output=True, text=True, timeout=3, check=False
            ).stdout.strip()
        except Exception:
            version = ""
        if not _print_result(t("doctor.check.tmux_version"), bool(version), version):
            problems += 1

    if not _print_result(
        t("doctor.check.python_version"),
        sys.version_info >= (3, 9),
        f"{sys.version_info.major}.{sys.version_info.minor}",
    ):
        problems += 1

    try:
        import curses as _curses  # noqa: F401

        _print_result(t("doctor.check.curses"), True)
    except Exception as exc:
        _print_result(t("doctor.check.curses"), False, str(exc))
        problems += 1

    term = os.environ.get("TERM", "")
    if term:
        _print_result(t("doctor.check.term"), True)
    else:
        _print_result(t("doctor.check.term"), False, t("doctor.term_missing"), warn=True)
        problems += 1

    session = tmux_capture.current_session()
    if session:
        _print_result(t("doctor.check.in_tmux"), True)
    else:
        _print_result(t("doctor.check.in_tmux"), False, t("doctor.not_in_tmux"), warn=True)
        problems += 1

    panes_output = tmux_capture.run_tmux(["list-panes", "-a"])
    if not _print_result(t("doctor.check.server"), bool(panes_output) or session != ""):
        problems += 1

    hosts = load_remote_hosts()
    print()
    if hosts:
        print(t("doctor.remote_hosts_configured", n=len(hosts), hosts=", ".join(h["alias"] for h in hosts)))
    else:
        print(t("doctor.remote_hosts_none"))

    print()
    print(t("doctor.summary_ok") if problems == 0 else t("doctor.summary_fail", n=problems))

    return 0 if problems == 0 else 1


def focus_active_tower() -> None:
    """Jump to whichever pane is currently registered as the active Tower
    for this session -- does NOT start a new Tower. Meant for a
    ``Ctrl+b w``-style binding (``run-shell -b "tower --focus"``): plain
    tmux navigation commands only, no curses, so it's safe to run
    backgrounded from a key binding.

    Never falls back to matching a window by name (see
    ``tmux/registration.py``'s module docstring for the bug that caused).
    """

    session = tmux_capture.current_session()
    if not session:
        print(t("cli.no_tmux_session"), file=sys.stderr)
        raise SystemExit(1)

    pane_id = registration.resolve_active_pane(session)
    if not pane_id:
        tmux_capture.display_message(t("cli.no_active_tower"))
        return

    registration.focus_pane(pane_id)


def has_active_tower() -> int:
    """Exit status for ``tower --has-active``.

    0 when this session has a live registered Tower pane, 1 otherwise.
    Prints nothing and does not start a TUI or move the client. A stale
    registration is cleared by ``resolve_active_pane``.
    """

    session = tmux_capture.current_session()
    if not session:
        return 1
    return 0 if registration.resolve_active_pane(session) else 1


def run_keys(action: str) -> int:
    try:
        if action == "install":
            keybind.install_for_user()
            print(t("cli.keys.installed"))
            return 0
        if action == "restore":
            keybind.restore_for_user()
            print(t("cli.keys.restored"))
            return 0
    except Exception:
        print(t("cli.keys.failed"), file=sys.stderr)
        return 1
    print(t("cli.keys.failed"), file=sys.stderr)
    return 1


def run_serve(lan: bool, port: int, use_tailscale: bool = False) -> None:
    """``tower serve``: foreground wrapper around ``server.service``.

    The TUI starts the same function in a detached process. Binding rules
    live there -- ``--tailscale`` never binds ``0.0.0.0``.
    """

    # Resolve the tmux session here, in the process the user launched from.
    # The server lifecycle receives it explicitly and never re-infers it.
    session = tmux_capture.current_session()
    if not session:
        print(t("cli.no_tmux_session"), file=sys.stderr)
        raise SystemExit(1)
    own_pane_id = tmux_capture.current_pane_id()

    mode = "tailscale" if use_tailscale else ("lan" if lan else "local")
    code = remote_service.serve_foreground(mode, port, detached=False, session=session, own_pane_id=own_pane_id)
    if code:
        raise SystemExit(code)


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="tower", description="A local-first TUI control tower for monitoring coding agents across tmux panes.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--doctor", action="store_true", help="run environment diagnostics and exit")
    update_group = parser.add_mutually_exclusive_group()
    update_group.add_argument("--check-update", action="store_true", help="check the official GitHub Release for an update")
    update_group.add_argument("--update", action="store_true", help="review and apply an official Release update")
    update_group.add_argument("--migrate-update", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="show an update plan without changing files")
    parser.add_argument("--channel", choices=("auto", "stable", "rc"), default="auto", help="Release channel (default: follow the installed version track)")
    parser.add_argument(
        "--here",
        action="store_true",
        help="deprecated, no-op: running with no flags already runs in the current pane",
    )
    parser.add_argument(
        "--focus",
        action="store_true",
        help="jump to the currently active Tower pane; does not start a new Tower (for a Ctrl+b w-style binding)",
    )
    parser.add_argument(
        "--has-active",
        action="store_true",
        help="exit 0 when a live Tower pane is registered, 1 otherwise; prints nothing",
    )

    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser(
        "serve", help="start Tower Remote: a small local web UI + API for checking status from your phone"
    )
    serve_network_group = serve_parser.add_mutually_exclusive_group()
    serve_network_group.add_argument(
        "--lan",
        action="store_true",
        help="bind to all interfaces so devices on your LAN can connect (default: localhost only)",
    )
    serve_network_group.add_argument(
        "--tailscale",
        action="store_true",
        help="expose over your tailnet via a Windows-host Tailscale Serve HTTPS mapping (backend stays localhost-only)",
    )
    serve_parser.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT, help=f"port to listen on (default: {DEFAULT_SERVE_PORT})")

    keys_parser = subparsers.add_parser("keys", help="install or remove the opt-in smart Ctrl+b w binding")
    keys_parser.add_argument("action", choices=("install", "restore"))

    connect_parser = subparsers.add_parser(
        "connect", help="connect to a Tower host through your existing SSH configuration"
    )
    connect_parser.add_argument("target", nargs="?", help="an OpenSSH host alias or address")
    connect_parser.add_argument(
        "--session", default="", help="choose a registered Tower session when more than one is active"
    )
    connect_internal = connect_parser.add_mutually_exclusive_group()
    connect_internal.add_argument("--_register-client", metavar="ID", help=argparse.SUPPRESS)
    connect_internal.add_argument("--_unregister-client", metavar="ID", help=argparse.SUPPRESS)
    connect_internal.add_argument("--_resolve-session", nargs="?", const="", metavar="SESSION", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    update_action = args.check_update or args.update or args.migrate_update
    if args.dry_run and not (args.update or args.migrate_update):
        parser.error("--dry-run requires --update or the migration command")
    if args.channel != "auto" and not update_action:
        parser.error("--channel requires an update command")
    if update_action and (args.version or args.doctor or args.here or args.focus or args.has_active or args.command):
        parser.error("update commands cannot be combined with other Tower commands")
    if update_action:
        from .update_manager import run_cli

        action = "check" if args.check_update else "update"
        raise SystemExit(
            run_cli(
                action,
                current_facts=runtime_facts(),
                requested_channel=args.channel,
                dry_run=args.dry_run,
                bootstrap=args.migrate_update,
                argv0=sys.argv[0],
            )
        )

    if args.command == "connect":
        from .connect import connect, register_from_stdin, resolve_active_tower_session

        if args._register_client:
            raise SystemExit(register_from_stdin(args._register_client))
        if args._unregister_client:
            raise SystemExit(register_from_stdin(args._unregister_client, unregister=True))
        if args._resolve_session is not None:
            raise SystemExit(resolve_active_tower_session(args._resolve_session))
        if not args.target:
            connect_parser.error("target is required")
        raise SystemExit(connect(args.target, args.session))

    _warn_if_foreign()

    if args.command == "serve":
        run_serve(args.lan, args.port, use_tailscale=args.tailscale)
        return

    if args.version:
        print(f"tower {__version__}")
        return

    if args.doctor:
        raise SystemExit(run_doctor())

    if args.has_active:
        raise SystemExit(has_active_tower())

    if args.focus:
        focus_active_tower()
        return

    if args.command == "keys":
        raise SystemExit(run_keys(args.action))

    run_ui()


if __name__ == "__main__":
    cli()
