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
from .tmux import capture as tmux_capture
from .ui.tower import CONTROL_WINDOW, run as run_ui, load_remote_hosts


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


def open_control_tower() -> None:
    """Ensure a CONTROL window exists in the target session and switch to it.

    Mirrors what the user would do by hand: create the window if missing,
    then ``select-window``. Never touches any other window/pane.
    """

    in_tmux = bool(os.environ.get("TMUX"))
    session = tmux_capture.current_session()

    if not session:
        session = tmux_capture.run_tmux(
            [
                "list-sessions",
                "-F",
                "#{session_name}|#{session_attached}|#{session_activity}",
            ]
        )
        best = ""
        best_key = (-1, -1)
        for line in session.split("\n"):
            parts = line.split("|")
            if len(parts) != 3:
                continue
            name, attached, activity = parts
            try:
                key = (int(attached), int(activity))
            except ValueError:
                key = (0, 0)
            if key > best_key:
                best_key = key
                best = name
        session = best

    if not session:
        print(t("cli.no_session_found"), file=sys.stderr)
        raise SystemExit(1)

    windows = tmux_capture.list_windows(session)
    if CONTROL_WINDOW not in windows:
        tower_exe = shutil.which("tower")
        launch_cmd = f"{tower_exe} --here" if tower_exe else f"{sys.executable} -m tmux_agent_tower.main --here"
        tmux_capture.new_control_window(session, CONTROL_WINDOW, launch_cmd)

    if in_tmux:
        tmux_capture.select_window(session, CONTROL_WINDOW)
    else:
        tmux_capture.select_window(session, CONTROL_WINDOW)
        os.execvp("tmux", ["tmux", "attach-session", "-t", session])


def cli(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="tower", description="A local-first TUI control tower for monitoring coding agents across tmux panes.")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--doctor", action="store_true", help="run environment diagnostics and exit")
    parser.add_argument(
        "--here",
        action="store_true",
        help="run the TUI in the current pane (used internally by the CONTROL window)",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"tower {__version__}")
        return

    if args.doctor:
        raise SystemExit(run_doctor())

    if args.here:
        run_ui()
        return

    open_control_tower()


if __name__ == "__main__":
    cli()
