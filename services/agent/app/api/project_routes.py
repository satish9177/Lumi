"""Milestone 10 S3 routes: registered projects, trusted recipes and supervised runs.

Runtime-internal (main holds the only bearer credential and pins each path). No route takes a command,
an executable, arguments, a shell or a path to run; the one route that takes a folder path is project
registration, which main calls only with the folder a native dialog returned and the person confirmed.
Responses never carry a path: a project is an id and a label.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import ErrorResponse
from app.domain.projects import MAX_ENV_ENTRIES, MAX_ENV_VALUE_CHARS
from app.services.projects import ProjectService, ProjectView, RecipeView, RunView

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterProjectBody(_Body):
    path: str = Field(min_length=3, max_length=1024)
    label: str = Field(min_length=1, max_length=64)


class RevisionBody(_Body):
    expected_revision: int = Field(ge=1)


class CreateRecipeBody(_Body):
    label: str = Field(min_length=1, max_length=64)
    script: str = Field(min_length=1, max_length=64)
    readiness_kind: Literal["http", "exit_code"]
    ready_port: int | None = Field(default=None, ge=1024, le=65535)
    ready_path: str | None = Field(default=None, max_length=128)
    timeout_seconds: int = Field(ge=5, le=600)
    env: dict[str, Annotated[str, Field(max_length=MAX_ENV_VALUE_CHARS)]] = Field(default_factory=dict, max_length=MAX_ENV_ENTRIES)
    #: Exactly what the native confirmation showed; a recipe is refused if package.json moved meanwhile.
    confirmed_script_text: str = Field(max_length=2000)
    confirmed_pre_text: str | None = Field(default=None, max_length=2000)
    confirmed_post_text: str | None = Field(default=None, max_length=2000)


class CreateRunBody(_Body):
    recipe_id: uuid.UUID


class RunGrantBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class RunRevokeBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1)


class ProjectResponse(BaseModel):
    project_id: uuid.UUID
    label: str
    revision: int
    created_at: datetime

    @classmethod
    def from_view(cls, view: ProjectView) -> "ProjectResponse":
        return cls(project_id=view.id, label=view.label, revision=view.revision, created_at=view.created_at)


class ScriptResponse(BaseModel):
    name: str
    text: str


class ScriptListResponse(BaseModel):
    scripts: list[ScriptResponse]


class RecipeResponse(BaseModel):
    recipe_id: uuid.UUID
    project_id: uuid.UUID
    label: str
    script: str
    script_text: str
    pre_text: str | None
    post_text: str | None
    env_names: list[str]
    readiness_kind: str
    ready_port: int | None
    ready_path: str | None
    timeout_seconds: int
    status: str
    invalid_reason: str | None
    revision: int

    @classmethod
    def from_view(cls, view: RecipeView) -> "RecipeResponse":
        return cls(
            recipe_id=view.id, project_id=view.project_id, label=view.label, script=view.script, script_text=view.script_text,
            pre_text=view.pre_text, post_text=view.post_text, env_names=list(view.env_names), readiness_kind=view.readiness_kind,
            ready_port=view.ready_port, ready_path=view.ready_path, timeout_seconds=view.timeout_seconds, status=view.status,
            invalid_reason=view.invalid_reason, revision=view.revision,
        )


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]


class RecipeListResponse(BaseModel):
    recipes: list[RecipeResponse]


class RunCardResponse(BaseModel):
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    recipe_id: uuid.UUID
    recipe_revision: int
    project_label: str
    label: str
    script: str
    script_text: str
    pre_text: str | None
    post_text: str | None
    argv: list[str]
    env_names: list[str]
    readiness_kind: str
    ready_port: int | None
    ready_path: str | None
    timeout_seconds: int
    stop_policy: str
    warning: str


class RunResponse(BaseModel):
    """Safe metadata: no path, no pid, no environment values. The log is untrusted text for display only."""

    task_id: uuid.UUID
    task_status: str
    task_revision: int
    phase: Literal[
        "awaiting_approval", "approved", "declined", "expired", "starting", "running", "ready", "succeeded", "failed",
        "stopped", "ended_with_runtime", "outcome_unknown",
    ]
    start_status: str | None
    error_code: str | None
    exit_code: int | None
    active_processes: int
    ready: bool
    log_tail: list[str]
    card: RunCardResponse | None

    @classmethod
    def from_view(cls, view: RunView) -> "RunResponse":
        grant = view.grant
        run = view.run
        card = None
        if grant is not None:
            scope = grant.scope
            card = RunCardResponse(
                grant_id=grant.id, grant_revision=grant.revision, grant_status=grant.status.value, expires_at=grant.expires_at,
                recipe_id=scope.recipe_id, recipe_revision=scope.recipe_revision, project_label=scope.project_label,
                label=scope.label, script=scope.script, script_text=scope.script_text, pre_text=scope.pre_text,
                post_text=scope.post_text, argv=list(scope.argv), env_names=list(scope.env_names),
                readiness_kind=scope.readiness_kind, ready_port=scope.ready_port, ready_path=scope.ready_path,
                timeout_seconds=scope.timeout_seconds, stop_policy=scope.stop_policy, warning=scope.warning,
            )
        return cls(
            task_id=view.task_id, task_status=view.task_status, task_revision=view.task_revision, phase=view.phase,
            start_status=view.start_status, error_code=run.error_code if run else None,
            exit_code=run.exit_code if run else None, active_processes=view.active_processes, ready=view.ready,
            log_tail=list(view.log_tail), card=card,
        )


class LatestRunResponse(BaseModel):
    run: RunResponse | None


def get_project_service(request: Request) -> ProjectService:
    service: ProjectService = request.app.state.project_service
    return service


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


@router.post("/projects", status_code=status.HTTP_201_CREATED, response_model=ProjectResponse, responses=_RESPONSES,
             summary="Register a project folder main got from a native dialog plus the execution warning")
async def register_project(body: RegisterProjectBody, service: ProjectServiceDep) -> ProjectResponse:
    return ProjectResponse.from_view(await service.register_project(path=body.path, label=body.label))


@router.get("/projects", response_model=ProjectListResponse, summary="Registered projects")
async def list_projects(service: ProjectServiceDep) -> ProjectListResponse:
    return ProjectListResponse(projects=[ProjectResponse.from_view(view) for view in await service.list_projects()])


@router.post("/projects/{project_id}/revoke", response_model=ProjectResponse, responses=_RESPONSES, summary="Remove a project")
async def revoke_project(project_id: uuid.UUID, body: RevisionBody, service: ProjectServiceDep) -> ProjectResponse:
    return ProjectResponse.from_view(await service.revoke_project(project_id, expected_revision=body.expected_revision))


@router.get("/projects/{project_id}/scripts", response_model=ScriptListResponse, responses=_RESPONSES,
            summary="The scripts package.json declares (read-only)")
async def list_scripts(project_id: uuid.UUID, service: ProjectServiceDep) -> ScriptListResponse:
    return ScriptListResponse(scripts=[ScriptResponse(name=s.name, text=s.text) for s in await service.list_scripts(project_id)])


@router.post("/projects/{project_id}/recipes", status_code=status.HTTP_201_CREATED, response_model=RecipeResponse, responses=_RESPONSES,
             summary="Register a recipe (trusted UI only)")
async def create_recipe(project_id: uuid.UUID, body: CreateRecipeBody, service: ProjectServiceDep) -> RecipeResponse:
    return RecipeResponse.from_view(
        await service.create_recipe(
            project_id=project_id, label=body.label, script=body.script, readiness_kind=body.readiness_kind,
            ready_port=body.ready_port, ready_path=body.ready_path, timeout_seconds=body.timeout_seconds, env=body.env,
            confirmed_texts=(body.confirmed_script_text, body.confirmed_pre_text, body.confirmed_post_text),
        )
    )


@router.get("/project-recipes", response_model=RecipeListResponse, summary="Registered recipes")
async def list_recipes(service: ProjectServiceDep) -> RecipeListResponse:
    return RecipeListResponse(recipes=[RecipeResponse.from_view(view) for view in await service.list_recipes()])


@router.post("/project-recipes/{recipe_id}/revoke", response_model=RecipeResponse, responses=_RESPONSES, summary="Remove a recipe")
async def revoke_recipe(recipe_id: uuid.UUID, body: RevisionBody, service: ProjectServiceDep) -> RecipeResponse:
    return RecipeResponse.from_view(await service.revoke_recipe(recipe_id, expected_revision=body.expected_revision))


@router.post("/project-runs", status_code=status.HTTP_201_CREATED, response_model=RunResponse, responses=_RESPONSES,
             summary="Open the per-run card for one recipe. Nothing runs")
async def create_run(body: CreateRunBody, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.create_run(recipe_id=body.recipe_id))


@router.get("/project-runs/latest", response_model=LatestRunResponse, summary="The newest run, if any")
async def latest_run(service: ProjectServiceDep) -> LatestRunResponse:
    view = await service.latest()
    return LatestRunResponse(run=None if view is None else RunResponse.from_view(view))


@router.get("/project-runs/{task_id}", response_model=RunResponse, responses=_RESPONSES, summary="One run's status")
async def get_run(task_id: uuid.UUID, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.describe(task_id))


@router.post("/project-runs/{task_id}/grant", response_model=RunResponse, responses=_RESPONSES, summary="The trusted click (R3)")
async def grant_run(task_id: uuid.UUID, body: RunGrantBody, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.confirm(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision))


@router.post("/project-runs/{task_id}/revoke", response_model=RunResponse, responses=_RESPONSES, summary="Decline before start")
async def revoke_run(task_id: uuid.UUID, body: RunRevokeBody, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.revoke(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision))


@router.post("/project-runs/{task_id}/start", response_model=RunResponse, responses=_RESPONSES, summary="Start once")
async def start_run(task_id: uuid.UUID, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.start(task_id))


@router.post("/project-runs/{task_id}/stop", response_model=RunResponse, responses=_RESPONSES,
             summary="Terminate this run's job, and only it")
async def stop_run(task_id: uuid.UUID, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.stop(task_id))


@router.post("/project-runs/{task_id}/reconcile", response_model=RunResponse, responses=_RESPONSES,
             summary="Read-only: settle a run whose state is unknown. Never starts anything")
async def reconcile_run(task_id: uuid.UUID, service: ProjectServiceDep) -> RunResponse:
    return RunResponse.from_view(await service.reconcile(task_id))
