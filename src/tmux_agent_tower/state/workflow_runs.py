"""Durable, human-controlled workflow run state."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from ..launcher.plan import WorkspaceRef
from ..launcher.presets import WorkspacePreset
from ..launcher.workflows import HumanGateStage, RoleStage, WorkflowPreset
from .overrides import ROLE_IDS

RUN_FILE_NAME = "workflow-runs.json"
STAGE_STATES = ("PENDING", "RUNNING", "PASS", "FAIL", "BLOCKED")
_VERSION = 1


class WorkflowRunError(ValueError):
    """A run transition or persisted workflow run is invalid."""


@dataclass(frozen=True)
class PaneIdentity:
    pane_key: str
    session: str
    pane_pid: str


@dataclass(frozen=True)
class WorkflowStageRun:
    kind: str
    role_id: Optional[str] = None
    state: str = "PENDING"
    started_at: str = ""
    finished_at: str = ""
    pane: Optional[PaneIdentity] = None
    provider: str = ""
    task_label: str = ""


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    created_at: str
    updated_at: str
    workspace_id: str
    workspace_name: str
    host_key: str
    host_label: str
    is_remote: bool
    workspaces: Tuple[WorkspaceRef, ...]
    agents: Tuple[str, ...]
    layout: str
    workflow_id: str
    workflow_name: str
    stages: Tuple[WorkflowStageRun, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class WorkflowRunStore:
    """Atomic local JSON store; task briefs and captured output are never fields."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def list(self) -> list[WorkflowRun]:
        return list(_decode_runs(self._read_document()))

    def get(self, run_id: str) -> WorkflowRun:
        for run in self.list():
            if run.run_id == run_id:
                return run
        raise WorkflowRunError("Workflow 실행을 찾을 수 없습니다.")

    def create(
        self,
        workspace: WorkspacePreset,
        workflow: WorkflowPreset,
        *,
        run_id: str = "",
    ) -> WorkflowRun:
        now = _now()
        stages = tuple(
            WorkflowStageRun("role", stage.role_id)
            if isinstance(stage, RoleStage)
            else WorkflowStageRun("human_gate")
            for stage in workflow.stages
        )
        run = WorkflowRun(
            run_id=run_id or uuid.uuid4().hex,
            created_at=now,
            updated_at=now,
            workspace_id=workspace.preset_id,
            workspace_name=workspace.name,
            host_key=workspace.host_key,
            host_label=workspace.host_label,
            is_remote=workspace.is_remote,
            workspaces=tuple(workspace.workspaces),
            agents=tuple(workspace.agents),
            layout=workspace.layout,
            workflow_id=workflow.preset_id,
            workflow_name=workflow.name,
            stages=stages,
        )
        _validate_run(run)
        document = self._read_document()
        runs = list(_decode_runs(document))
        if any(item.run_id == run.run_id for item in runs):
            raise WorkflowRunError("중복된 Workflow 실행 ID입니다.")
        runs.append(run)
        self._write_runs(runs)
        return run

    def start_stage(
        self,
        run_id: str,
        stage_index: int,
        *,
        pane: Optional[PaneIdentity] = None,
        provider: str = "",
        task_label: str = "",
    ) -> WorkflowRun:
        run = self.get(run_id)
        _check_next_stage(run, stage_index)
        stage = run.stages[stage_index]
        if stage.kind == "role":
            if pane is None or not isinstance(pane, PaneIdentity) or not all(
                isinstance(value, str) and value for value in (pane.pane_key, pane.session, pane.pane_pid)
            ):
                raise WorkflowRunError("pane 신원을 확인한 뒤 역할 단계를 시작하세요.")
            if not isinstance(provider, str) or not provider.strip() or not isinstance(task_label, str) or not task_label.strip():
                raise WorkflowRunError("선택한 Agent와 작업을 확인할 수 없습니다.")
        elif (
            pane is not None
            or not isinstance(provider, str)
            or provider
            or not isinstance(task_label, str)
            or task_label
        ):
            raise WorkflowRunError("사람 확인 단계에는 pane, Agent, 작업 입력을 연결할 수 없습니다.")
        updated_stage = replace(
            stage,
            state="RUNNING",
            started_at=_now(),
            finished_at="",
            pane=pane,
            provider=provider.strip(),
            task_label=task_label.strip(),
        )
        return self._save_stage(run, stage_index, updated_stage)

    def finish_stage(self, run_id: str, stage_index: int, outcome: str) -> WorkflowRun:
        if outcome not in ("PASS", "FAIL", "BLOCKED"):
            raise WorkflowRunError("단계 결과는 PASS, FAIL, BLOCKED 중에서 선택하세요.")
        run = self.get(run_id)
        _check_index(run, stage_index)
        stage = run.stages[stage_index]
        if stage.state != "RUNNING":
            raise WorkflowRunError("실행 중인 단계만 결과를 기록할 수 있습니다.")
        return self._save_stage(run, stage_index, replace(stage, state=outcome, finished_at=_now()))

    def retry_stage(self, run_id: str, stage_index: int) -> WorkflowRun:
        run = self.get(run_id)
        _check_index(run, stage_index)
        if any(stage.state == "RUNNING" for stage in run.stages):
            raise WorkflowRunError("실행 중인 단계가 있어 다시 시도할 수 없습니다.")
        stage = run.stages[stage_index]
        if stage.state not in ("FAIL", "BLOCKED"):
            raise WorkflowRunError("실패 또는 차단된 단계만 다시 시도할 수 있습니다.")
        return self._save_stage(
            run,
            stage_index,
            replace(stage, state="PENDING", started_at="", finished_at="", pane=None, provider="", task_label=""),
        )

    def _save_stage(self, run: WorkflowRun, index: int, stage: WorkflowStageRun) -> WorkflowRun:
        stages = list(run.stages)
        stages[index] = stage
        updated = replace(run, updated_at=_now(), stages=tuple(stages))
        _validate_run(updated)
        document = self._read_document()
        runs = list(_decode_runs(document))
        for position, current in enumerate(runs):
            if current.run_id == run.run_id:
                runs[position] = updated
                self._write_runs(runs)
                return updated
        raise WorkflowRunError("Workflow 실행이 저장소에서 사라졌습니다.")

    def _read_document(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": _VERSION, "runs": []}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowRunError("Workflow 실행 파일을 읽을 수 없습니다. 기존 파일은 보존했습니다.") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "runs"}
            or type(document.get("version")) is not int
            or document.get("version") != _VERSION
            or not isinstance(document.get("runs"), list)
        ):
            raise WorkflowRunError("Workflow 실행 파일 형식이 올바르지 않습니다. 기존 파일은 보존했습니다.")
        return document

    def _write_runs(self, runs: list[WorkflowRun]) -> None:
        temp_path = None
        fd = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = None
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": _VERSION, "runs": [_encode_run(run) for run in runs]}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise WorkflowRunError("Workflow 실행을 안전하게 저장하지 못했습니다.") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def _check_index(run: WorkflowRun, index: int) -> None:
    if type(index) is not int or not 0 <= index < len(run.stages):
        raise WorkflowRunError("Workflow 단계를 찾을 수 없습니다.")


def _check_next_stage(run: WorkflowRun, index: int) -> None:
    _check_index(run, index)
    if any(stage.state == "RUNNING" for stage in run.stages):
        raise WorkflowRunError("이미 실행 중인 단계가 있습니다.")
    if any(stage.state != "PASS" for stage in run.stages[:index]):
        raise WorkflowRunError("앞 단계가 모두 PASS여야 다음 단계를 시작할 수 있습니다.")
    if run.stages[index].state != "PENDING":
        raise WorkflowRunError("PENDING 상태인 단계만 시작할 수 있습니다.")


def _validate_run(run: WorkflowRun) -> None:
    if not isinstance(run.run_id, str) or not run.run_id or any(ord(char) < 32 for char in run.run_id):
        raise WorkflowRunError("Workflow 실행 ID가 올바르지 않습니다.")
    if not isinstance(run.created_at, str) or not run.created_at or not isinstance(run.updated_at, str) or not run.updated_at:
        raise WorkflowRunError("Workflow 실행 시각이 올바르지 않습니다.")
    if not isinstance(run.stages, tuple) or not run.stages or len(run.stages) > 100:
        raise WorkflowRunError("Workflow 단계 목록이 올바르지 않습니다.")
    identity_text = (run.workspace_id, run.workspace_name, run.host_key, run.host_label, run.layout, run.workflow_id, run.workflow_name)
    if not all(isinstance(value, str) and value for value in identity_text):
        raise WorkflowRunError("Workspace 참조가 올바르지 않습니다.")
    if type(run.is_remote) is not bool:
        raise WorkflowRunError("Workspace Host 값이 올바르지 않습니다.")
    if not run.workspaces or any(
        not isinstance(ref, WorkspaceRef)
        or not isinstance(ref.name, str)
        or not ref.name
        or not isinstance(ref.path, str)
        or not ref.path
        for ref in run.workspaces
    ):
        raise WorkflowRunError("Workspace 경로 정보가 올바르지 않습니다.")
    if not isinstance(run.agents, tuple) or any(not isinstance(agent, str) or not agent for agent in run.agents) or len(set(run.agents)) != len(run.agents):
        raise WorkflowRunError("Workspace Agent 목록이 올바르지 않습니다.")
    running = 0
    found_non_pass = False
    for stage in run.stages:
        if not isinstance(stage, WorkflowStageRun):
            raise WorkflowRunError("저장된 Workflow 단계 형식이 올바르지 않습니다.")
        if stage.kind == "role":
            if stage.role_id not in ROLE_IDS:
                raise WorkflowRunError("저장된 역할 단계가 올바르지 않습니다.")
        elif stage.kind == "human_gate":
            if stage.role_id is not None or stage.pane is not None or stage.provider or stage.task_label:
                raise WorkflowRunError("사람 확인 단계에 Agent 정보가 포함되었습니다.")
        else:
            raise WorkflowRunError("저장된 Workflow 단계 형식이 올바르지 않습니다.")
        if stage.state not in STAGE_STATES:
            raise WorkflowRunError("저장된 Workflow 단계 상태가 올바르지 않습니다.")
        if found_non_pass and stage.state != "PENDING":
            raise WorkflowRunError("앞 단계가 끝나기 전에 뒤 단계 상태가 변경되었습니다.")
        if stage.state != "PASS":
            found_non_pass = True
        if stage.state == "RUNNING":
            running += 1
        if stage.state == "PENDING":
            if stage.started_at or stage.finished_at:
                raise WorkflowRunError("대기 단계에 실행 시각이 남아 있습니다.")
            if stage.pane is not None or stage.provider or stage.task_label:
                raise WorkflowRunError("대기 단계에 이전 pane 선택 정보가 남아 있습니다.")
        elif not stage.started_at:
            raise WorkflowRunError("실행 단계의 시작 시각이 없습니다.")
        if stage.state in ("RUNNING", "PENDING") and stage.finished_at:
            raise WorkflowRunError("완료되지 않은 단계에 종료 시각이 있습니다.")
        if stage.state in ("PASS", "FAIL", "BLOCKED") and not stage.finished_at:
            raise WorkflowRunError("종료 단계의 결과 시각이 없습니다.")
        if stage.kind == "role" and stage.state in ("RUNNING", "PASS", "FAIL", "BLOCKED"):
            if stage.pane is None or not stage.provider or not stage.task_label:
                raise WorkflowRunError("실행 단계의 pane 신원을 확인할 수 없습니다.")
            if not all(isinstance(value, str) and value for value in (
                stage.pane.pane_key, stage.pane.session, stage.pane.pane_pid
            )):
                raise WorkflowRunError("실행 단계의 pane 신원이 올바르지 않습니다.")
        if stage.kind == "human_gate" and stage.pane is not None:
            raise WorkflowRunError("사람 확인 단계에 pane이 연결되었습니다.")
    if running > 1:
        raise WorkflowRunError("실행 중인 Workflow 단계가 둘 이상입니다.")


def _encode_run(run: WorkflowRun) -> dict:
    return {
        "run_id": run.run_id,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "workspace": {
            "id": run.workspace_id,
            "name": run.workspace_name,
            "host_key": run.host_key,
            "host_label": run.host_label,
            "is_remote": run.is_remote,
            "workspaces": [{"name": ref.name, "path": ref.path} for ref in run.workspaces],
            "agents": list(run.agents),
            "layout": run.layout,
        },
        "workflow": {"id": run.workflow_id, "name": run.workflow_name},
        "stages": [_encode_stage(stage) for stage in run.stages],
    }


def _encode_stage(stage: WorkflowStageRun) -> dict:
    encoded = {
        "kind": stage.kind,
        "state": stage.state,
        "started_at": stage.started_at,
        "finished_at": stage.finished_at,
    }
    if stage.kind == "role":
        encoded.update({
            "role_id": stage.role_id,
            "pane": None if stage.pane is None else {
                "pane_key": stage.pane.pane_key,
                "session": stage.pane.session,
                "pane_pid": stage.pane.pane_pid,
            },
            "provider": stage.provider,
            "task_label": stage.task_label,
        })
    return encoded


def _decode_runs(document: dict) -> Tuple[WorkflowRun, ...]:
    try:
        runs = tuple(_decode_run(item) for item in document["runs"])
        ids = [run.run_id for run in runs]
        if len(ids) != len(set(ids)):
            raise WorkflowRunError("Workflow 실행 ID가 중복되었습니다. 기존 파일은 보존했습니다.")
        return runs
    except WorkflowRunError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowRunError("저장된 Workflow 실행이 손상되었습니다. 기존 파일은 보존했습니다.") from exc


def _decode_run(item: dict) -> WorkflowRun:
    if not isinstance(item, dict) or set(item) != {"run_id", "created_at", "updated_at", "workspace", "workflow", "stages"}:
        raise TypeError("unexpected run fields")
    workspace = item["workspace"]
    workflow = item["workflow"]
    if not isinstance(workspace, dict) or set(workspace) != {
        "id", "name", "host_key", "host_label", "is_remote", "workspaces", "agents", "layout"
    }:
        raise TypeError("unexpected workspace fields")
    if not isinstance(workflow, dict) or set(workflow) != {"id", "name"}:
        raise TypeError("unexpected workflow fields")
    if (
        type(workspace["is_remote"]) is not bool
        or not isinstance(workspace["workspaces"], list)
        or not isinstance(workspace["agents"], list)
        or not all(isinstance(value, str) for value in workspace["agents"])
        or not all(isinstance(workflow[key], str) for key in ("id", "name"))
    ):
        raise TypeError("invalid workspace or workflow values")
    refs = tuple(WorkspaceRef(row["name"], row["path"]) for row in workspace["workspaces"])
    stages = tuple(_decode_stage(stage) for stage in item["stages"])
    run = WorkflowRun(
        run_id=item["run_id"], created_at=item["created_at"], updated_at=item["updated_at"],
        workspace_id=workspace["id"], workspace_name=workspace["name"], host_key=workspace["host_key"],
        host_label=workspace["host_label"], is_remote=workspace["is_remote"], workspaces=refs,
        agents=tuple(workspace["agents"]), layout=workspace["layout"],
        workflow_id=workflow["id"], workflow_name=workflow["name"], stages=stages,
    )
    if not all(isinstance(value, str) for value in (run.created_at, run.updated_at)):
        raise TypeError("invalid timestamps")
    _validate_run(run)
    return run


def _decode_stage(item: dict) -> WorkflowStageRun:
    common = {"kind", "state", "started_at", "finished_at"}
    if not isinstance(item, dict) or not common.issubset(item):
        raise TypeError("unexpected stage fields")
    if item["kind"] == "human_gate":
        if set(item) != common:
            raise TypeError("Human Gate cannot store Agent fields")
        if not all(isinstance(item[key], str) for key in common):
            raise TypeError("invalid Human Gate values")
        return WorkflowStageRun("human_gate", state=item["state"], started_at=item["started_at"], finished_at=item["finished_at"])
    role_fields = common | {"role_id", "pane", "provider", "task_label"}
    if set(item) != role_fields:
        raise TypeError("unexpected role stage fields")
    pane_value = item["pane"]
    if pane_value is not None:
        if not isinstance(pane_value, dict) or set(pane_value) != {"pane_key", "session", "pane_pid"}:
            raise TypeError("unexpected pane identity fields")
        pane = PaneIdentity(pane_value["pane_key"], pane_value["session"], pane_value["pane_pid"])
    else:
        pane = None
    if not all(isinstance(item[key], str) for key in ("kind", "state", "started_at", "finished_at", "provider", "task_label")):
        raise TypeError("invalid stage values")
    if item["role_id"] is not None and not isinstance(item["role_id"], str):
        raise TypeError("invalid role")
    return WorkflowStageRun(item["kind"], item["role_id"], item["state"], item["started_at"], item["finished_at"], pane, item["provider"], item["task_label"])
