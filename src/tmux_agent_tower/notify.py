"""Best-effort desktop notifications for status transitions.

Off by default (see ``launcher/config.py``'s ``notifications`` key) --
when disabled, nothing in this module is ever called, and Tower itself
has zero dependency on any notification backend being available.

Backends, in order: ``notify-send`` (Linux desktop), falling back to
``tmux display-message`` (works anywhere tmux does, including a plain
WSL/SSH session with no desktop notification daemon at all). Windows/WSL
toast notifications were not implemented -- that would need a native
Windows-side helper (e.g. BurntToast or a small .NET/PowerShell bridge)
that a headless WSL Python process cannot invoke on its own with zero
extra setup; left as a documented gap rather than a hard dependency (see
docs/ROADMAP.md).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Optional

from .i18n import t
from .tmux import capture as tmux_capture

APP_NAME = "Tmux Agent Tower"


def send(message: str, title: str = APP_NAME) -> None:
    """Fire-and-forget: never raises, never blocks the UI for long."""

    notify_send = shutil.which("notify-send")
    if notify_send:
        try:
            subprocess.run(
                [notify_send, title, message],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
            return
        except Exception:
            pass

    # Fallback: always available wherever tmux is, no desktop needed.
    tmux_capture.display_message(f"{title}: {message}")


@dataclass
class NotificationTracker:
    """One-shot-per-transition bookkeeping, kept separate from
    ``StatusEngine`` (which tracks status for *display*, not for deciding
    when to notify) so a disabled notifications config can skip this
    entirely with no effect on status reporting itself.
    """

    enabled: bool = False
    notify_waiting: bool = True
    notify_dead: bool = False
    _last_status: Dict[str, str] = field(default_factory=dict)

    def observe(self, pane_id: str, status: str, project_name: str) -> Optional[str]:
        """Records ``status`` for ``pane_id`` and fires a notification if
        this is a genuine transition worth mentioning. Returns the message
        sent (for tests), or ``None`` if nothing was sent.

        A pane seen for the very first time is recorded but never
        notified about -- otherwise every pre-existing WAITING pane would
        fire a notification the moment Tower starts, which is noise, not
        a transition.
        """

        previous = self._last_status.get(pane_id)
        self._last_status[pane_id] = status

        if not self.enabled or previous is None or previous == status:
            return None

        message = None

        if status == "WAITING" and self.notify_waiting:
            message = t("notify.waiting", project=project_name)
        elif status == "DEAD" and self.notify_dead:
            message = t("notify.dead", project=project_name)

        if message:
            send(message)

        return message

    def forget(self, pane_id: str) -> None:
        self._last_status.pop(pane_id, None)
