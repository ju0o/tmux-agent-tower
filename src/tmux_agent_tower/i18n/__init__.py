"""Minimal i18n layer.

Business logic and internal state constants (``STATUS_WORKING``,
``VISIT_NEW``, etc. in ``detection/status.py`` and ``state/visits.py``)
stay in English identifiers always -- only this module's ``t()`` function
turns a key into user-facing text. This keeps status/visit comparisons
(``if status == STATUS_WORKING``) stable regardless of display language.

Current default is Korean (see docs/ROADMAP.md: a first-run KO/EN picker
and ``tower config`` are public-release i18n work, not part of the current
dogfood scope). ``en`` is fully populated too so the structure is ready for
that later step without a find-and-replace pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import ko, en

_CATALOGS = {"ko": ko.STRINGS, "en": en.STRINGS}
_DEFAULT_LANGUAGE = "ko"

CONFIG_FILE = Path.home() / ".config" / "tmux-agent-tower" / "config.toml"

_LANGUAGE_LINE_RE = re.compile(r'^\s*language\s*=\s*"?([A-Za-z]{2})"?\s*$')


def _detect_language() -> str:
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            match = _LANGUAGE_LINE_RE.match(line)
            if match:
                candidate = match.group(1).lower()
                if candidate in _CATALOGS:
                    return candidate
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return _DEFAULT_LANGUAGE


_current_language = _detect_language()


def set_language(language: Optional[str]) -> None:
    global _current_language
    if language in _CATALOGS:
        _current_language = language


def current_language() -> str:
    return _current_language


def t(key: str, **kwargs) -> str:
    catalog = _CATALOGS.get(_current_language, _CATALOGS[_DEFAULT_LANGUAGE])
    text = catalog.get(key)
    if text is None:
        text = _CATALOGS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text
