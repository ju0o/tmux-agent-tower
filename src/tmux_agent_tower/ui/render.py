"""Pure, curses-free rendering/formatting decisions for the Tower TUI.

Kept separate from ``ui/tower.py`` so what-to-show-and-when logic (project
name priority, when a pane title is worth a second line, responsive
thresholds, search filtering) can be unit tested without a real curses
screen. Nothing here imports ``curses``.
"""

from __future__ import annotations

import unicodedata
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..detection.attention import card_rank

from ..detection.project import is_low_confidence_name


def display_width(text: str) -> int:
    """Terminal column width of ``text``, counting East Asian Wide/Fullwidth
    characters (Korean, CJK, many box-drawing/symbol glyphs) as 2 columns.
    Plain ``len()`` undercounts these, which silently broke right-alignment
    and truncation math throughout this Korean-first UI -- a real bug
    caught live (host summary text clipped, detail-panel labels
    misaligned) once rows started mixing Korean and English text on the
    same line.
    """

    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_to_width(text: str, max_width: int) -> str:
    """Truncates ``text`` to at most ``max_width`` terminal columns,
    appending an ellipsis if anything was cut -- width-aware, so it never
    cuts a wide character in half or undercounts them (see
    ``display_width``).
    """

    if max_width <= 0:
        return ""

    if display_width(text) <= max_width:
        return text

    budget = max_width - 1  # room for the ellipsis
    out = []
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > budget:
            break
        out.append(ch)
        used += w

    return "".join(out) + "…"

STATUS_SYMBOL = {
    "WORKING": "●",
    "WAITING": "!",
    "IDLE": "○",
    "UNKNOWN": "?",
    "DEAD": "×",
}

STATUS_ORDER = ["WORKING", "WAITING", "IDLE", "UNKNOWN", "DEAD"]

# Below this width, agent + status move to their own line under the
# project name instead of sharing the first line.
NARROW_WIDTH_THRESHOLD = 70

# Below this height, the selected-item detail panel is hidden entirely so
# the pane list itself keeps as much room as possible.
SHORT_HEIGHT_THRESHOLD = 18


def use_narrow_layout(width: int) -> bool:
    return width < NARROW_WIDTH_THRESHOLD


def should_show_detail_panel(height: int) -> bool:
    return height >= SHORT_HEIGHT_THRESHOLD


def format_host_summary(counts: Dict[str, int], status_label: Callable[[str], str]) -> str:
    """``status_label(status) -> translated word``. A status with a zero
    count is omitted entirely -- a host with nothing waiting shouldn't
    have to say so.
    """

    parts = [
        f"{STATUS_SYMBOL[s]} {status_label(s)} {counts.get(s, 0)}"
        for s in STATUS_ORDER
        if counts.get(s, 0) > 0
    ]
    return "   ".join(parts)


def looks_meaningful_title(title: Optional[str], local_host: str) -> bool:
    """False for an empty title, the placeholder "(unnamed)", or a title
    that's just the host's own name (common for a freshly-opened shell
    pane, e.g. an SSH session titled after the remote hostname) -- none of
    those say anything about what the pane is actually for.
    """

    if not title:
        return False

    stripped = title.strip()

    if not stripped or stripped == "(unnamed)":
        return False

    if local_host and stripped.lower() == local_host.strip().lower():
        return False

    return True


def resolve_display_project(
    custom: Optional[str],
    git_name: Optional[str],
    title: Optional[str],
    basename: Optional[str],
    local_host: str,
    no_name_label: str,
) -> str:
    """The project name shown as a row's primary text.

    Priority: an explicit user override > the enclosing git repo's name >
    a meaningful pane title > the raw directory basename > an honest
    "no name" placeholder. A single-letter or generic mount-point-ish
    basename (see ``detection.project.is_low_confidence_name``) is never
    shown as-is if something better is available.
    """

    if custom:
        return custom

    if git_name and not is_low_confidence_name(git_name):
        return git_name

    if looks_meaningful_title(title, local_host):
        return title.strip()

    if basename and not is_low_confidence_name(basename):
        return basename

    return no_name_label


def title_secondary_line(project: str, title: Optional[str], local_host: str) -> Optional[str]:
    """The pane title shown as a secondary line under the project name --
    only when it's meaningful AND tells the user something the project
    name doesn't already say (never a duplicate line).
    """

    if not looks_meaningful_title(title, local_host):
        return None

    stripped = title.strip()

    if stripped.lower() == (project or "").lower():
        return None

    return stripped


def row_matches_filter(row: Dict, filter_text: str) -> bool:
    """Case-insensitive substring match across the fields a user would
    actually recognise a pane by. An empty filter matches everything.
    """

    if not filter_text:
        return True

    needle = filter_text.strip().lower()
    if not needle:
        return True

    haystacks = (
        row.get("project") or "",
        row.get("agent") or "",
        row.get("title_line") or "",
        row.get("path") or "",
        row.get("host") or "",
    )

    return any(needle in h.lower() for h in haystacks)


# WAITING and UNKNOWN need a decision from the user; DEAD is worth
# noticing; WORKING is already progressing fine; IDLE is the least
# interesting. Lower number = more attention-worthy.
ATTENTION_PRIORITY = {"WAITING": 0, "UNKNOWN": 1, "DEAD": 2, "WORKING": 3, "IDLE": 4}


def format_duration(seconds: float) -> str:
    """"Tower has continuously observed this status for ~N" -- never framed
    as "the agent started this N ago" (Tower cannot know that; see
    ``StatusEngine.duration_seconds``'s docstring). Coarse on purpose: a
    control tower doesn't need seconds-level precision once something's
    been running for minutes or hours.
    """

    seconds = max(0, int(seconds))

    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes:
        return f"{hours}h {remaining_minutes}m"
    return f"{hours}h"


def sort_by_attention(rows: List[Dict]) -> List[Dict]:
    """Attention view and the phone list share ``card_rank``.

    A stable sort, so equal ranks keep their existing order. The default
    list does not use this.
    """

    return sorted(rows, key=card_rank)


def row_line_count(row: Dict, narrow: bool) -> int:
    """How many physical terminal lines this row occupies: 1 for the
    primary project/agent/status line, +1 more in narrow layout (agent +
    status move to their own line, see ``agent_status_line``), +1 more if
    there's a meaningful pane-title secondary line to show, +1 more if
    there's a current-activity line to show. An empty/missing activity
    never forces a blank line (see ``ui/tower.py``'s row-building).
    """

    lines = 1
    if narrow:
        lines += 1
    if row.get("title_line"):
        lines += 1
    if row.get("activity_text"):
        lines += 1
    return lines


def agent_status_line(agent: str, status_label: str, duration_text: str = "") -> str:
    """Narrow-layout line 2: agent + status (+ duration, if shown), since
    there's no room for them beside the project name on line 1.
    """

    text = f"{agent}  {status_label}"
    if duration_text:
        text += f" · {duration_text}"
    return text


def format_detail_panel(fields: Sequence[Tuple[str, str]]) -> List[str]:
    """``fields`` is ``[(translated_label, value), ...]``; returns
    label-aligned display lines, e.g. ``"프로젝트    JuHome"``.

    Aligned by *display* width (see ``display_width``), not character
    count -- labels here routinely mix Korean ("프로젝트") and English
    ("Host"), which have different character-count-to-column ratios.
    """

    fields = [(label, value) for label, value in fields if value is not None]
    if not fields:
        return []

    label_width = max(display_width(label) for label, _ in fields)
    lines = []
    for label, value in fields:
        pad = " " * max(0, label_width - display_width(label))
        lines.append(f"{label}{pad}  {value}")
    return lines
