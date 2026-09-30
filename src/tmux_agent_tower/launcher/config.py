"""Workspace Launcher configuration.

Deliberately does NOT hardcode any user-specific path into the product
(see ``default_project_roots``). ``~/.config/tmux-agent-tower/config.toml``
is optional; everything here has a safe default so a fresh install works
with zero configuration.

This reads a *subset* of TOML sufficient for our own controlled file shape
(top-level string/array-of-strings keys, plus one ``[agents]`` table of
string values). It is not a general TOML parser -- adding a dependency for
that felt disproportionate to a zero-runtime-dependency local tool. If the
file doesn't parse, we fail safe to defaults rather than crashing.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_FILE = Path.home() / ".config" / "tmux-agent-tower" / "config.toml"

# Label used throughout the UI/adapters -> the config key a user would write
# in [agents] to override its launch command.
AGENT_LABEL_TO_CONFIG_KEY = {
    "Codex": "codex",
    "Claude": "claude",
    "OpenCode": "opencode",
    "Grok": "grok",
    "Cursor": "cursor",
    "Shell": "shell",
}

AGENT_LAUNCH_ORDER = ["Codex", "Claude", "OpenCode", "Grok", "Cursor", "Shell"]


def _default_shell() -> str:
    import os

    return os.environ.get("SHELL") or "bash"


def default_agent_commands() -> Dict[str, str]:
    return {
        "Codex": "codex",
        "Claude": "claude",
        "OpenCode": "opencode",
        "Grok": "grok",
        # tmux/ps report cursor-agent's own process as "agent" (see
        # adapters/cursor.py) but the command a user actually types is
        # "cursor-agent".
        "Cursor": "cursor-agent",
        "Shell": _default_shell(),
    }


# Never assume any specific user's directory layout exists -- only offer a
# root if it's actually there.
_CANDIDATE_ROOT_NAMES = ["Projects", "projects", "code", "Code", "dev", "src", "workspace", "Workspace"]


def default_project_roots() -> List[str]:
    home = Path.home()
    return [str(home / name) for name in _CANDIDATE_ROOT_NAMES if (home / name).is_dir()]


_ARRAY_RE = re.compile(r'^\s*project_roots\s*=\s*\[(.*)\]\s*$')
_SECTION_RE = re.compile(r'^\s*\[(\w+)\]\s*$')
_KV_RE = re.compile(r'^\s*(\w+)\s*=\s*"([^"]*)"\s*$')
_STRING_ITEM_RE = re.compile(r'"([^"]*)"')


def _parse_config_text(text: str) -> Dict:
    result: Dict = {"project_roots": None, "agents": {}}
    section: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0]
        if not line.strip():
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1)
            continue

        if section is None:
            array_match = _ARRAY_RE.match(line)
            if array_match:
                items = _STRING_ITEM_RE.findall(array_match.group(1))
                result["project_roots"] = [str(Path(p).expanduser()) for p in items]
                continue

        kv_match = _KV_RE.match(line)
        if kv_match:
            key, value = kv_match.group(1), kv_match.group(2)
            if section == "agents":
                result["agents"][key] = value
            elif section is None:
                result[key] = value

    return result


def load_config() -> Dict:
    """Returns ``{"language": str|None, "project_roots": [...], "agents": {Label: command}}``."""

    agents = default_agent_commands()
    project_roots: Optional[List[str]] = None
    language = None

    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        parsed = _parse_config_text(text)
        if parsed.get("project_roots"):
            project_roots = parsed["project_roots"]
        language = parsed.get("language")

        config_key_to_label = {v: k for k, v in AGENT_LABEL_TO_CONFIG_KEY.items()}
        for config_key, command in parsed.get("agents", {}).items():
            label = config_key_to_label.get(config_key)
            if label and command:
                agents[label] = command
    except FileNotFoundError:
        pass
    except Exception:
        # Malformed config: fail safe to defaults.
        pass

    if project_roots is None:
        project_roots = default_project_roots()

    return {"language": language, "project_roots": project_roots, "agents": agents}


def resolve_agent_command(agent_label: str, agents_cfg: Dict[str, str]) -> Optional[str]:
    """Full command string if its binary is on PATH, else ``None``."""

    command = agents_cfg.get(agent_label)
    if not command:
        return None

    binary = command.split()[0]
    if shutil.which(binary) is None:
        return None

    return command
