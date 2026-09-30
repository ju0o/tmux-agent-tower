"""``tower`` CLI entry point."""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import subprocess
import sys
import threading
import time

from . import __version__
from .i18n import t
from .server import httpapi, netutil
from .tmux import capture as tmux_capture
from .tmux import registration
from .ui.tower import run as run_ui, load_remote_hosts

DEFAULT_SERVE_PORT = 4312


def _print_result(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    mark = "✓" if ok else ("!" if warn else "✗")
    status_word = t("doctor.ok") if ok else detail or t("doctor.fail")
    line = f"{mark} {label:<22} {status_word}"
    print(line)
    return ok


def run_doctor() -> int:
    print(t("doctor.title"))
    print()

    problems = 0

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


def run_serve(lan: bool, port: int) -> None:
    """``tower serve``: Tower Remote's tiny read/prompt-send HTTP API +
    mobile web UI (see ``server/httpapi.py`` for the security boundary).

    Binds to localhost only unless ``--lan`` is explicitly given -- never
    LAN-reachable by default (see docs/ARCHITECTURE.md's Tower Remote
    section for why an explicit opt-in matters here).
    """

    session = tmux_capture.current_session()
    if not session:
        print(t("cli.no_tmux_session"), file=sys.stderr)
        raise SystemExit(1)

    own_pane_id = tmux_capture.current_pane_id()

    host = "0.0.0.0" if lan else "127.0.0.1"
    allowed_hosts = ["localhost", "127.0.0.1"]
    lan_ip = netutil.detect_lan_ip() if lan else None
    if lan_ip:
        allowed_hosts.append(lan_ip)

    try:
        server = httpapi.create_server(host, port, session, own_pane_id, allowed_hosts)
    except OSError as exc:
        print(f"Could not start Tower Remote on port {port}: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("TMUX AGENT TOWER REMOTE")
    print()
    print(f"Local:  http://127.0.0.1:{port}")
    if lan:
        if lan_ip:
            print(f"LAN:    http://{lan_ip}:{port}")
        else:
            print("LAN:    (could not detect a LAN IP address on this machine)")
    else:
        print("(localhost only -- pass --lan to allow your phone to connect)")
    print()
    code = server.pairing.current_code()
    print(f"Pairing code: {code}  (valid 5 minutes, one-time use)")
    print("Open the address above on your phone's browser and enter this code.")
    print()
    print("Type 'r' + Enter any time for a new pairing code. Ctrl+C to stop.")
    sys.stdout.flush()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    stdin_thread = threading.Thread(target=_watch_stdin_for_regenerate, args=(server,), daemon=True)
    stdin_thread.start()

    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print("Stopping Tower Remote...")
    finally:
        server.shutdown()
        server.server_close()


def _watch_stdin_for_regenerate(server) -> None:
    """Local-only pairing code regeneration: typing 'r' + Enter in the
    same terminal ``tower serve`` is running in. Deliberately never
    exposed over HTTP (see ``server/httpapi.py`` -- no endpoint calls
    ``PairingSession.regenerate()``), since a remote client being able to
    mint itself a fresh pairing code on demand would defeat the whole
    point of rate-limiting wrong guesses.
    """

    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return  # stdin closed (e.g. running under a non-interactive wrapper)
        if line.strip().lower() == "r":
            new_code = server.pairing.regenerate()
            print(f"\nNew pairing code: {new_code}  (valid 5 minutes, one-time use)")
            sys.stdout.flush()


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="tower", description="A local-first TUI control tower for monitoring coding agents across tmux panes.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--doctor", action="store_true", help="run environment diagnostics and exit")
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

    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser(
        "serve", help="start Tower Remote: a small local web UI + API for checking status from your phone"
    )
    serve_parser.add_argument(
        "--lan",
        action="store_true",
        help="bind to all interfaces so devices on your LAN can connect (default: localhost only)",
    )
    serve_parser.add_argument("--port", type=int, default=DEFAULT_SERVE_PORT, help=f"port to listen on (default: {DEFAULT_SERVE_PORT})")

    args = parser.parse_args(argv)

    if args.command == "serve":
        run_serve(args.lan, args.port)
        return

    if args.version:
        print(f"tower {__version__}")
        return

    if args.doctor:
        raise SystemExit(run_doctor())

    if args.focus:
        focus_active_tower()
        return

    run_ui()


if __name__ == "__main__":
    cli()
