"""``tower`` CLI entry point."""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import subprocess
import sys

from . import __version__
from .tmux import capture as tmux_capture
from .ui.tower import CONTROL_WINDOW, run as run_ui, load_remote_hosts


def _print_check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {label}"
    if detail:
        line += f" - {detail}"
    print(line)
    return ok


def _print_warn(label: str, detail: str = "") -> None:
    line = f"[WARN] {label}"
    if detail:
        line += f" - {detail}"
    print(line)


def run_doctor() -> int:
    all_ok = True

    tmux_path = shutil.which("tmux")
    all_ok &= _print_check("tmux installed", bool(tmux_path), tmux_path or "not found on PATH")

    if tmux_path:
        try:
            version = subprocess.run(
                ["tmux", "-V"], capture_output=True, text=True, timeout=3, check=False
            ).stdout.strip()
        except Exception:
            version = ""
        all_ok &= _print_check("tmux version readable", bool(version), version)

    all_ok &= _print_check(
        "python >= 3.9",
        sys.version_info >= (3, 9),
        f"{sys.version_info.major}.{sys.version_info.minor}",
    )

    try:
        import curses as _curses  # noqa: F401

        all_ok &= _print_check("python curses module available", True)
    except Exception as exc:
        all_ok &= _print_check("python curses module available", False, str(exc))

    term = os.environ.get("TERM", "")
    if term:
        _print_check("TERM set", True, term)
    else:
        _print_warn("TERM not set", "curses may not initialize correctly")

    session = tmux_capture.current_session()
    if session:
        _print_check("running inside a tmux session", True, session)
    else:
        _print_warn(
            "not running inside tmux",
            "tower needs to run inside a tmux client session",
        )

    panes_output = tmux_capture.run_tmux(["list-panes", "-a"])
    _print_check("tmux server reachable", bool(panes_output) or session != "", "")

    hosts = load_remote_hosts()
    if hosts:
        print(f"[INFO] {len(hosts)} remote host(s) configured: " + ", ".join(h["alias"] for h in hosts))
    else:
        print("[INFO] no remote hosts configured (single-host mode) - see docs/ARCHITECTURE.md")

    return 0 if all_ok else 1


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
        print("No tmux session found. Start tmux first.", file=sys.stderr)
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
