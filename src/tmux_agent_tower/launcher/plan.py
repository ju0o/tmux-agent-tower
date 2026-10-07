"""Validated workspace launch plans shared by guided and saved launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ..launcher.browse import validate_local_path, validate_remote_path
from ..state.bindings import ProjectBindingStore
from ..tmux import structure
from .config import AGENT_LABEL_TO_CONFIG_KEY, load_config
from .discovery import record_recent
from .spawn import (
    LAYOUT_CHOICES,
    SpawnTarget,
    launch_window_name,
    spawn_into_window,
    spawn_local,
    spawn_remote,
)


@dataclass(frozen=True)
class WorkspaceRef:
    name: str
    path: str


@dataclass(frozen=True)
class LaunchPlan:
    host_key: str
    host_label: str
    is_remote: bool
    workspaces: Tuple[WorkspaceRef, ...]
    agents: Tuple[str, ...]
    layout: str
    placement: str = "new"
    window_id: str = ""
    remote_session: str = ""
    pane_direction: str = "auto"


class LaunchPlanError(ValueError):
    """A plan was incomplete, stale, or unsupported before any spawn."""


def validate_launch_plan(plan: LaunchPlan) -> None:
    if not isinstance(plan, LaunchPlan):
        raise LaunchPlanError("실행 계획 형식이 올바르지 않습니다.")
    if type(plan.is_remote) is not bool or not isinstance(plan.host_key, str) or not isinstance(plan.host_label, str):
        raise LaunchPlanError("실행 컴퓨터 정보가 올바르지 않습니다.")
    if not plan.host_key.strip() or not plan.host_label.strip():
        raise LaunchPlanError("실행 컴퓨터를 선택하세요.")
    if any(ch.isspace() or ord(ch) < 32 for ch in plan.host_key):
        raise LaunchPlanError("실행 컴퓨터 이름을 확인할 수 없습니다.")
    if plan.is_remote and (plan.host_key.startswith("-") or not all(
        ch.isalnum() or ch in "_.-" for ch in plan.host_key
    )):
        raise LaunchPlanError("원격 호스트 이름을 확인할 수 없습니다.")
    if not plan.workspaces:
        raise LaunchPlanError("작업공간을 하나 이상 선택하세요.")
    seen_paths = set()
    for workspace in plan.workspaces:
        if not isinstance(workspace, WorkspaceRef) or not isinstance(workspace.name, str) or not isinstance(workspace.path, str):
            raise LaunchPlanError("작업공간 정보가 올바르지 않습니다.")
        if not workspace.name.strip() or not workspace.path.strip():
            raise LaunchPlanError("작업공간 이름과 경로를 확인하세요.")
        if any(ord(ch) < 32 for ch in workspace.name + workspace.path):
            raise LaunchPlanError("작업공간 이름이나 경로에 사용할 수 없는 문자가 있습니다.")
        if workspace.path in seen_paths:
            raise LaunchPlanError("같은 작업공간이 여러 번 선택되었습니다.")
        seen_paths.add(workspace.path)
    if not isinstance(plan.agents, tuple) or not plan.agents or any(
        not isinstance(agent, str) or agent not in AGENT_LABEL_TO_CONFIG_KEY for agent in plan.agents
    ):
        raise LaunchPlanError("실행할 Agent를 하나 이상 선택하세요.")
    if len(set(plan.agents)) != len(plan.agents):
        raise LaunchPlanError("같은 Agent가 여러 번 선택되었습니다.")
    if not isinstance(plan.layout, str) or plan.layout not in LAYOUT_CHOICES.values():
        raise LaunchPlanError(f"지원하지 않는 작업 배치입니다: {plan.layout}")
    if plan.placement not in {"new", "current", "bound", "pick"}:
        raise LaunchPlanError("작업을 놓을 위치를 확인할 수 없습니다.")
    if plan.pane_direction not in {"auto", "horizontal", "vertical"}:
        raise LaunchPlanError("새 작업의 방향을 확인할 수 없습니다.")
    if plan.is_remote and plan.placement in {"bound", "pick"}:
        raise LaunchPlanError("원격 작업은 새 작업 묶음이나 선택한 기존 묶음에만 만들 수 있습니다.")
    if plan.placement in {"current", "bound", "pick"} and not plan.is_remote:
        if not structure.valid_window_id(plan.window_id):
            raise LaunchPlanError("선택한 작업 묶음을 확인할 수 없습니다.")
    if plan.placement == "current" and plan.is_remote:
        if len(plan.workspaces) * len(plan.agents) != 1:
            raise LaunchPlanError("원격의 기존 작업 묶음에는 Agent 하나만 추가할 수 있습니다.")
        if not plan.remote_session or not structure.valid_window_id(plan.window_id):
            raise LaunchPlanError("원격 작업 묶음을 다시 선택해야 합니다.")
        if any(ord(ch) < 32 for ch in plan.remote_session):
            raise LaunchPlanError("원격 작업 묶음을 확인할 수 없습니다.")


def execute_launch_plan(
    plan: LaunchPlan,
    *,
    session: str,
    state_dir: Path,
    agents_cfg: Optional[dict] = None,
    overrides=None,
) -> list[SpawnResult]:
    """Validate every target first, then create only resources in this plan."""

    validate_launch_plan(plan)
    cfg = agents_cfg if agents_cfg is not None else load_config()["agents"]
    missing = [agent for agent in plan.agents if not cfg.get(agent)]
    if missing:
        raise LaunchPlanError("실행 명령이 없는 Agent가 있습니다: " + ", ".join(missing))

    checked = []
    for workspace in plan.workspaces:
        result = (
            validate_remote_path(plan.host_key, workspace.path)
            if plan.is_remote
            else validate_local_path(workspace.path)
        )
        if not result.ok or result.entry is None:
            raise LaunchPlanError(f"작업공간을 확인할 수 없습니다: {workspace.path}")
        checked.append(WorkspaceRef(result.entry.name, result.entry.path))

    targets = [
        SpawnTarget(workspace.path, workspace.name, agent)
        for workspace in checked
        for agent in plan.agents
    ]
    for workspace in checked:
        record_recent(state_dir, plan.host_key, workspace.path)

    if plan.is_remote:
        destination = (
            (plan.remote_session, plan.window_id)
            if plan.placement == "current"
            else None
        )
        return spawn_remote(
            plan.host_key,
            launch_window_name(targets),
            targets,
            cfg,
            layout=plan.layout,
            destination=destination,
        )

    bindings = ProjectBindingStore(state_dir / "project-bindings.json")
    pane_placement = (
        plan.placement in {"current", "bound", "pick"}
        and len(targets) == 1
        and bool(plan.window_id)
    )
    if pane_placement:
        return [
            spawn_into_window(
                session,
                plan.window_id,
                targets[0],
                cfg,
                bindings=bindings,
                overrides=overrides,
                direction=plan.pane_direction,
            )
        ]
    return spawn_local(
        session,
        targets,
        cfg,
        layout=plan.layout,
        bindings=bindings,
        overrides=overrides,
    )
