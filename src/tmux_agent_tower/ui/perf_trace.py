"""Opt-in, content-free timing events for diagnosing TUI input latency."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import time
from functools import wraps
from threading import Lock, current_thread

_path = os.environ.get("TOWER_INPUT_TRACE")
_lock = Lock()
_stream = open(_path, "a", encoding="utf-8", buffering=1) if _path else None


def event(name: str, **fields) -> None:
    if not _path:
        return
    item = {"event": name, "t_ns": time.monotonic_ns(), "thread": current_thread().name}
    item.update(fields)
    try:
        with _lock:
            _stream.write(json.dumps(item, separators=(",", ":")) + "\n")
    except OSError:
        pass


def draw(view: str):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            event("DRAW_BEGIN", view=view)
            try:
                return fn(*args, **kwargs)
            finally:
                event("DRAW_END", view=view)

        return wrapped

    return decorate


def refresh(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        event("DATA_REFRESH_BEGIN")
        try:
            return fn(*args, **kwargs)
        finally:
            event("DATA_REFRESH_COMPLETE")

    return wrapped


def _flush() -> None:
    if _stream is None:
        return
    try:
        with _lock:
            _stream.close()
    except OSError:
        pass


if _path:
    _popen = subprocess.Popen

    class _TracedPopen(_popen):
        def __init__(self, args, *more, **kwargs):
            command = args[0] if isinstance(args, (list, tuple)) and args else args
            program = os.path.basename(str(command).split()[0]) if command else "unknown"
            event("EXTERNAL_CALL", program=program)
            super().__init__(args, *more, **kwargs)

    subprocess.Popen = _TracedPopen
    atexit.register(_flush)
