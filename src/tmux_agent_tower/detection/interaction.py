"""One interaction shape for every agent.

Attention stays the axis (approval, input, error). This is the detail
under that axis: the current question and only the keys an adapter has
already verified. Nothing here sends a key, picks a default, or reads
scrollback above the widget the adapter passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass(frozen=True)
class InteractionOption:
    label: str
    key: str
    safe: bool = True

    def as_dict(self) -> dict:
        return {"label": self.label, "key": self.key, "safe": self.safe}


@dataclass(frozen=True)
class Interaction:
    """``type`` is choice, confirm, approval, text, or unknown."""

    type: str
    prompt: str
    options: Tuple[InteractionOption, ...] = ()
    input_allowed: bool = False
    source: str = ""
    confidence: str = "low"

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "prompt": self.prompt,
            "options": [option.as_dict() for option in self.options],
            "input_allowed": self.input_allowed,
            "source": self.source,
            "confidence": self.confidence,
        }

    def verified_key(self, key: str) -> Optional[str]:
        """The key only when this snapshot lists it as safe."""

        for option in self.options:
            if option.safe and option.key == key:
                return option.key
        return None


def approval(
    prompt: str,
    options: Sequence[InteractionOption],
    source: str,
    confidence: str = "high",
) -> Interaction:
    safe = tuple(option for option in options if option.safe and option.key)
    if not safe:
        return Interaction("unknown", prompt, source=source, confidence="low")
    return Interaction("approval", prompt, safe, source=source, confidence=confidence)


def choice(prompt: str, options: Sequence[InteractionOption], source: str) -> Interaction:
    safe = tuple(option for option in options if option.safe and option.key)
    if len(safe) < 2:
        return Interaction("unknown", prompt, source=source, confidence="low")
    return Interaction("choice", prompt, safe, source=source, confidence="high")


def confirm(
    prompt: str,
    yes: Optional[InteractionOption],
    no: Optional[InteractionOption],
    source: str,
) -> Interaction:
    if not yes or not no or not yes.safe or not no.safe or not yes.key or not no.key:
        return Interaction("confirm", prompt, source=source, confidence="low")
    return Interaction("confirm", prompt, (yes, no), source=source, confidence="high")


def text_question(prompt: str, source: str) -> Interaction:
    return Interaction("text", prompt, input_allowed=True, source=source, confidence="medium")


def unknown(prompt: str, source: str) -> Interaction:
    return Interaction("unknown", prompt, source=source, confidence="low")
