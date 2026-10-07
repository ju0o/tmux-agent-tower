"""Code-defined role recipes, kept separate from workspace launch presets."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Union

from .presets import PRESET_FILE
from ..state.overrides import ROLE_IDS

CUSTOM_WORKFLOW_FILE = PRESET_FILE.with_name("workflow-presets.json")
MAX_CUSTOM_WORKFLOW_BYTES = 64 * 1024
_CUSTOM_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")


@dataclass(frozen=True)
class RoleStage:
    """A workflow stage assigned to a Tower role, never an Agent provider."""

    role_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.role_id, str) or self.role_id not in ROLE_IDS:
            raise ValueError("지원하지 않는 작업 역할입니다.")


@dataclass(frozen=True)
class HumanGateStage:
    """An explicit human-owned step; it has no Agent role or command."""


WorkflowStage = Union[RoleStage, HumanGateStage]


@dataclass(frozen=True)
class WorkflowPreset:
    preset_id: str
    name: str
    stages: Tuple[WorkflowStage, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.preset_id, str)
            or not self.preset_id
            or len(self.preset_id) > 80
            or self.preset_id != self.preset_id.strip()
            or any(ord(char) < 32 for char in self.preset_id)
            or not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 80
            or any(ord(char) < 32 for char in self.name)
            or not isinstance(self.stages, tuple)
            or not self.stages
        ):
            raise ValueError("작업 순서 이름과 단계가 필요합니다.")
        if any(type(stage) not in (RoleStage, HumanGateStage) for stage in self.stages):
            raise ValueError("작업 순서는 역할 또는 사람 확인 단계만 포함할 수 있습니다.")
        stage_keys = [
            ("role", stage.role_id) if type(stage) is RoleStage else ("human_gate", "")
            for stage in self.stages
        ]
        if len(stage_keys) != len(set(stage_keys)):
            raise ValueError("작업 순서에는 중복된 단계를 넣을 수 없습니다.")


class WorkflowPresetFileError(ValueError):
    """The local custom workflow JSON is invalid or unsafe to load."""


def _unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def load_custom_workflows(path: Path | None = None) -> Tuple[WorkflowPreset, ...]:
    """Read a bounded, data-only workflow file; never import or evaluate it."""

    source = Path(path) if path is not None else CUSTOM_WORKFLOW_FILE
    try:
        if source.is_symlink():
            raise WorkflowPresetFileError("symlink")
        with source.open("rb") as stream:
            raw = stream.read(MAX_CUSTOM_WORKFLOW_BYTES + 1)
        if len(raw) > MAX_CUSTOM_WORKFLOW_BYTES:
            raise WorkflowPresetFileError("oversized")
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_keys,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError:
        return ()
    except WorkflowPresetFileError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise WorkflowPresetFileError("invalid") from exc

    try:
        if type(document) is not dict or set(document) != {"version", "workflows"}:
            raise ValueError("shape")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("version")
        rows = document["workflows"]
        if type(rows) is not list:
            raise ValueError("workflows")

        result = []
        ids = {preset.preset_id for preset in WORKFLOW_PRESETS}
        names = {unicodedata.normalize("NFC", preset.name).casefold() for preset in WORKFLOW_PRESETS}
        for row in rows:
            if type(row) is not dict or set(row) != {"id", "name", "stages"}:
                raise ValueError("workflow fields")
            preset_id, name, stages = row["id"], row["name"], row["stages"]
            if (
                not isinstance(preset_id, str)
                or not _CUSTOM_ID.fullmatch(preset_id)
                or preset_id in ids
                or not isinstance(name, str)
                or not name
                or name != name.strip()
                or len(name) > 80
                or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in name)
                or unicodedata.normalize("NFC", name).casefold() in names
                or type(stages) is not list
                or not stages
            ):
                raise ValueError("workflow value")

            workflow_stages = []
            for stage_id in stages:
                if not isinstance(stage_id, str):
                    raise ValueError("stage type")
                if stage_id == "human_gate":
                    workflow_stages.append(HumanGateStage())
                elif stage_id in ROLE_IDS:
                    workflow_stages.append(RoleStage(stage_id))
                else:
                    raise ValueError("unknown stage")
            preset = WorkflowPreset(preset_id, name, tuple(workflow_stages))
            result.append(preset)
            ids.add(preset_id)
            names.add(unicodedata.normalize("NFC", name).casefold())
        return tuple(result)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowPresetFileError("invalid") from exc


def _roles(*role_ids: str, human_gate: bool = False) -> Tuple[WorkflowStage, ...]:
    stages: Tuple[WorkflowStage, ...] = tuple(RoleStage(role_id) for role_id in role_ids)
    return stages + ((HumanGateStage(),) if human_gate else ())


WORKFLOW_PRESETS = (
    WorkflowPreset("quick-fix", "빠른 수정", _roles("builder", "qa", human_gate=True)),
    WorkflowPreset("feature-development", "기능 개발", _roles("planner", "builder", "reviewer", "qa")),
    WorkflowPreset(
        "focused-improvement",
        "집중 고도화",
        _roles("orchestrator", "planner", "builder", "qa", "dogfood", "e2e", human_gate=True),
    ),
)
