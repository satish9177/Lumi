"""Milestone 10 S3: registered projects, trusted recipes and supervised runs.

What this service can do, and nothing else:

* register a project root the person chose in a native dialog and confirmed with the warning
  "This recipe executes code from this project with your user-level permissions." (Electron main);
* register a recipe for it, in trusted UI: one script the project's `package.json` declares, run by the
  pinned `node.exe` + `npm-cli.js` under Program Files, with a fixed env allowlist, readiness, timeout and
  the one stop policy (`terminate_job`). The recipe is frozen and identified by its digest;
* open a per-run card, confirm it (the trusted click, R3), and start the run ONCE:
  1. re-derive every fact the recipe froze -- any difference invalidates it (`recipe_changed`);
  2. check dependencies are present (read-only; a missing one is `missing_dependency`, never installed);
  3. commit the run row (a second live run of the project is refused by the database) and the attempt,
     with the `project_run` effect key;
  4. create the process suspended in its own Job Object, commit its (pid, creation time), then resume;
* report status (owned root alive, job process count, bounded untrusted log, readiness -- an HTTP port
  counts only when the listening socket's owner is IN the run's job);
* stop a run: `TerminateJobObject` on that run's job only.

There is no route that takes a command, an executable, arguments, a path to run, or a shell. Nothing here
installs dependencies or runs Git. A model can at most name a `recipe_id`.
"""

import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import tasks as tasks_table
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.effects import EffectKey, EffectKind, EffectLockedError
from app.domain.errors import TaskConcurrencyError, TaskKindMismatchError, TaskNotAcceptingActionsError, TaskNotFoundError
from app.domain.projects import (
    PROJECT_TASK_TYPE,
    TOOL_PROJECT_START,
    ProjectRefusal,
    ProjectRunScope,
    Readiness,
    RecipeSpec,
    run_effect_key_value,
    run_scope,
    spec_from_json,
    validate_env,
    validate_script_name,
    validate_timeout,
)
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.files.broker import FileBrokerRefusal, ProtectedFolders, inspect_root, verify_root
from app.files.handles import is_within, normcase
from app.projects.inspect import ancestor_npm_context, check_dependencies, package_facts, pinned_node
from app.projects.process import LaunchError, LaunchSpec, RunProcess, launch, listeners, process_is_alive
from app.repositories.actions import ActionRecord, ActionRepository
from app.repositories.files import FileRepository
from app.repositories.projects import ProjectRecord, ProjectRepository, RecipeRecord, RunGrantRecord, RunRecord
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.actions import ActionService

logger = logging.getLogger("lumi.projects")

STEP_TTL = timedelta(minutes=5)
_START_KEY = "project-start"
MAX_LABEL_CHARS = 64


def validate_label(value: object) -> str:
    if not isinstance(value, str):
        raise ProjectRefusal("label_invalid")
    label = value.strip()
    if not label or len(label) > MAX_LABEL_CHARS or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        raise ProjectRefusal("label_invalid")
    return label


def default_run_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Lumi", "project-runs")


def build_environment(*, run_dir: str, node_dir: str, extra: dict[str, str]) -> dict[str, str]:
    """The child's WHOLE environment, built from nothing: never a copy of the runtime's.

    It contains no provider key, no `LUMI_*`, no database URL, no cloud or Git credential, and points
    npm's user/global config and cache at empty Lumi-owned locations, so neither a `.npmrc` token nor
    anything in the person's own environment reaches the run. `ComSpec` is set because npm runs a
    package script through the Windows command processor; the script text is the recipe's, hashed.
    """
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    system32 = os.path.join(system_root, "System32")
    command_processor = os.path.join(system32, "cmd.exe")
    folders = {name: os.path.join(run_dir, name) for name in ("tmp", "home", "appdata", "localappdata", "npm-cache")}
    for folder in folders.values():
        os.makedirs(folder, exist_ok=True)
    npmrc = os.path.join(run_dir, "npmrc-user")
    global_npmrc = os.path.join(run_dir, "npmrc-global")
    for config in (npmrc, global_npmrc):
        if not os.path.exists(config):
            with open(config, "x", encoding="utf-8"):
                pass
    base = {
        "SystemRoot": system_root,
        "windir": system_root,
        "ComSpec": command_processor,
        "PATH": os.pathsep.join((node_dir, system32, system_root)),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": folders["tmp"],
        "TMP": folders["tmp"],
        "USERPROFILE": folders["home"],
        "HOME": folders["home"],
        "APPDATA": folders["appdata"],
        "LOCALAPPDATA": folders["localappdata"],
        "npm_config_userconfig": npmrc,
        "npm_config_globalconfig": global_npmrc,
        "npm_config_cache": folders["npm-cache"],
        "npm_config_offline": "true",
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        # npm ranks environment config above a project's own .npmrc (which Lumi refuses anyway): the shell
        # that runs the hashed script text is pinned to the system command processor.
        "npm_config_script_shell": command_processor,
        "NO_UPDATE_NOTIFIER": "1",
        # cmd.exe must not prefer an unhashed node.cmd/node.bat in the project folder over PATH (review finding 9).
        "NoDefaultCurrentDirectoryInExePath": "1",
    }
    return {**base, **extra}


@dataclass(frozen=True, slots=True)
class ProjectView:
    id: uuid.UUID
    label: str
    revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ScriptView:
    name: str
    text: str


@dataclass(frozen=True, slots=True)
class RecipeView:
    id: uuid.UUID
    project_id: uuid.UUID
    label: str
    script: str
    script_text: str
    pre_text: str | None
    post_text: str | None
    env_names: tuple[str, ...]
    readiness_kind: str
    ready_port: int | None
    ready_path: str | None
    timeout_seconds: int
    status: str
    invalid_reason: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class RunView:
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    phase: str
    grant: RunGrantRecord | None
    run: RunRecord | None
    start_status: str | None
    active_processes: int
    ready: bool
    log_tail: tuple[str, ...]


def _project_view(record: ProjectRecord) -> ProjectView:
    return ProjectView(id=record.id, label=record.label, revision=record.revision, created_at=record.created_at)


def _recipe_view(record: RecipeRecord) -> RecipeView:
    spec = record.spec
    readiness = spec.get("readiness") or {}
    return RecipeView(
        id=record.id, project_id=record.project_id, label=record.label, script=record.script_name,
        script_text=str(spec.get("script_text", "")), pre_text=spec.get("pre_text"), post_text=spec.get("post_text"),
        env_names=tuple(sorted(spec.get("env") or {})), readiness_kind=str(readiness.get("kind")),
        ready_port=readiness.get("port"), ready_path=readiness.get("path"), timeout_seconds=int(spec.get("timeout_seconds", 0)),
        status=record.status, invalid_reason=record.invalid_reason, revision=record.revision,
    )


class _RunAuthorizer:
    """Mints and consumes the one step authorization of a `project_run` grant, for `start_scoped_attempt`."""

    def __init__(self, grant: RunGrantRecord, runtime_generation: uuid.UUID) -> None:
        self._grant = grant
        self._runtime_generation = runtime_generation

    def authorization_payload(self) -> dict[str, Any]:
        return {
            "grant_id": str(self._grant.id), "grant_revision": self._grant.revision, "scope_digest": self._grant.scope_digest,
            "policy_version": self._grant.policy_version, "authorization": "task_grant",
        }

    async def mint(self, connection: AsyncConnection, *, action: ActionRecord) -> uuid.UUID:
        return await ProjectRepository(connection).insert_step_authorization(
            grant=self._grant, action_id=action.id, action_revision=action.revision, proposal_digest=action.proposal_digest,
            runtime_generation=self._runtime_generation, ttl=STEP_TTL,
        )

    async def consume(self, connection: AsyncConnection, *, authorization_id: uuid.UUID, action_revision: int, proposal_digest: str) -> bool:
        return await ProjectRepository(connection).consume_step_authorization(
            authorization_id=authorization_id, action_revision=action_revision, proposal_digest=proposal_digest,
            runtime_generation=self._runtime_generation,
        )


class ProjectService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        grant_ttl_seconds: int,
        forbidden_roots: tuple[str, ...] = (),
        protected_folders: ProtectedFolders | None = None,
        program_files: str | None = None,
        run_root: str | None = None,
        poll_seconds: float = 0.25,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._runtime_generation = runtime_generation
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)
        self._forbidden = forbidden_roots
        self._protected = protected_folders
        self._program_files = program_files
        self._run_root = run_root or default_run_root()
        self._poll = poll_seconds
        self._processes: dict[uuid.UUID, RunProcess] = {}
        self._monitors: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._starting: set[uuid.UUID] = set()
        #: The last lines of runs that ended in this runtime's lifetime (in memory only; never persisted).
        self._final_logs: dict[uuid.UUID, tuple[str, ...]] = {}

    # ---- projects -----------------------------------------------------------------------------------------

    async def register_project(self, *, path: str, label: object) -> ProjectView:
        """A folder the person chose in a native dialog and confirmed with the execution warning, in main."""
        name = validate_label(label)
        try:
            facts = await asyncio.to_thread(inspect_root, path, forbidden=self._forbidden, protected=self._protected)
        except FileBrokerRefusal as refusal:
            raise ProjectRefusal(refusal.code) from None
        await asyncio.to_thread(package_facts, facts.canonical_path)  # must be a Node project
        key = normcase(facts.canonical_path)
        async with self._engine.begin() as connection:
            # A folder Lumi may SAVE downloads into can never also be a folder Lumi RUNS code from.
            for root in await FileRepository(connection).list_roots():
                if root.active and root.can_create and (
                    normcase(root.canonical_path) == key or is_within(root.canonical_path, facts.canonical_path)
                    or is_within(facts.canonical_path, root.canonical_path)
                ):
                    raise ProjectRefusal("overlaps_download_folder")
            try:
                async with connection.begin_nested():
                    project = await ProjectRepository(connection).insert_project(
                        label=name, canonical_path=facts.canonical_path, path_key=key, volume=facts.volume, index=facts.index
                    )
            except IntegrityError:
                raise ProjectRefusal("project_already_registered") from None
        return _project_view(project)

    async def list_projects(self) -> list[ProjectView]:
        async with self._engine.connect() as connection:
            return [_project_view(project) for project in await ProjectRepository(connection).active_projects()]

    async def revoke_project(self, project_id: uuid.UUID, *, expected_revision: int) -> ProjectView:
        async with self._engine.begin() as connection:
            repository = ProjectRepository(connection)
            if await repository.live_run_for_project(project_id) is not None:
                raise ProjectRefusal("run_already_active")
            revoked = await repository.revoke_project(project_id, expected_revision=expected_revision)
            if revoked is None:
                raise ProjectRefusal("project_changed")
        return _project_view(revoked)

    async def _active_project(self, project_id: uuid.UUID) -> tuple[ProjectRecord, str]:
        async with self._engine.connect() as connection:
            project = await ProjectRepository(connection).get_project(project_id)
        if project is None:
            raise ProjectRefusal("project_not_found")
        if project.status != "ACTIVE":
            raise ProjectRefusal("project_revoked")
        try:
            canonical = await asyncio.to_thread(verify_root, project.canonical_path, volume=project.volume_serial, index=project.dir_index)
        except FileBrokerRefusal:
            raise ProjectRefusal("project_changed") from None
        return project, canonical

    async def list_scripts(self, project_id: uuid.UUID) -> list[ScriptView]:
        _, canonical = await self._active_project(project_id)
        facts = await asyncio.to_thread(package_facts, canonical)
        return [ScriptView(name=name, text=text) for name, text in sorted(facts.scripts.items())]

    # ---- recipes -------------------------------------------------------------------------------------------

    async def _derive(
        self, project: ProjectRecord, canonical: str, *, label: str, script: str, env: dict[str, str], readiness: Readiness,
        timeout_seconds: int,
    ) -> RecipeSpec:
        facts = await asyncio.to_thread(package_facts, canonical)
        if facts.npmrc_sha256 is not None:
            # npm reads the project's own .npmrc and it can change what runs (for example `node-options`, which
            # an empty environment override does NOT neutralise). Narrowed, not trusted: no project .npmrc.
            raise ProjectRefusal("project_npmrc_refused")
        if await asyncio.to_thread(ancestor_npm_context, canonical) is not None:
            raise ProjectRefusal("ancestor_npm_context")
        if script not in facts.scripts:
            raise ProjectRefusal("script_not_declared")
        for hook in (f"pre{script}", f"post{script}"):
            if hook in facts.runnable_names and hook not in facts.scripts:
                raise ProjectRefusal("hook_not_displayable")
        node, npm_cli = await asyncio.to_thread(pinned_node, self._program_files)
        return RecipeSpec(
            project_id=str(project.id), project_volume=project.volume_serial, project_index=project.dir_index, label=label,
            script=script, script_text=facts.scripts[script], pre_text=facts.scripts.get(f"pre{script}"),
            post_text=facts.scripts.get(f"post{script}"), package_sha256=facts.package_sha256,
            lockfile_name=facts.lockfile_name, lockfile_sha256=facts.lockfile_sha256, npmrc_sha256=facts.npmrc_sha256, node=node, npm_cli=npm_cli, env=env,
            readiness=readiness, timeout_seconds=timeout_seconds,
        )

    async def create_recipe(
        self, *, project_id: uuid.UUID, label: object, script: object, readiness_kind: object, ready_port: object,
        ready_path: object, timeout_seconds: object, env: object,
        confirmed_texts: tuple[str, str | None, str | None] | None = None,
    ) -> RecipeView:
        """Trusted UI only (the renderer's recipe form, confirmed natively in main)."""
        name = validate_label(label)
        script_name = validate_script_name(script)  # type: ignore[arg-type]
        readiness = Readiness.of(str(readiness_kind), ready_port if isinstance(ready_port, int) else None,
                                 ready_path if isinstance(ready_path, str) else None)
        timeout = validate_timeout(timeout_seconds)  # type: ignore[arg-type]
        variables = validate_env(env if isinstance(env, dict) else {})
        project, canonical = await self._active_project(project_id)
        spec = await self._derive(project, canonical, label=name, script=script_name, env=variables, readiness=readiness, timeout_seconds=timeout)
        if confirmed_texts is not None and confirmed_texts != (spec.script_text, spec.pre_text, spec.post_text):
            # package.json changed while the native confirmation was open (S3 review finding 3).
            raise ProjectRefusal("script_changed_during_review")
        async with self._engine.begin() as connection:
            recipe = await ProjectRepository(connection).insert_recipe(
                project_id=project.id, label=name, script_name=script_name, spec=asdict(spec), digest=spec.digest()
            )
        return _recipe_view(recipe)

    async def list_recipes(self, project_id: uuid.UUID | None = None) -> list[RecipeView]:
        async with self._engine.connect() as connection:
            return [_recipe_view(recipe) for recipe in await ProjectRepository(connection).recipes(project_id)]

    async def revoke_recipe(self, recipe_id: uuid.UUID, *, expected_revision: int) -> RecipeView:
        async with self._engine.begin() as connection:
            revoked = await ProjectRepository(connection).revoke_recipe(recipe_id, expected_revision=expected_revision)
            if revoked is None:
                raise ProjectRefusal("recipe_changed")
        return _recipe_view(revoked)

    async def _current_recipe(self, recipe_id: uuid.UUID) -> tuple[RecipeRecord, RecipeSpec, ProjectRecord, str]:
        """The recipe, re-derived from the disk NOW. Any difference invalidates it for good."""
        async with self._engine.connect() as connection:
            recipe = await ProjectRepository(connection).get_recipe(recipe_id)
        if recipe is None:
            raise ProjectRefusal("recipe_not_found")
        if recipe.status == "REVOKED":
            raise ProjectRefusal("recipe_revoked")
        if recipe.status != "ACTIVE":
            raise ProjectRefusal("recipe_changed")
        stored = spec_from_json(recipe.spec)
        if stored.digest() != recipe.digest:
            await self._invalidate(recipe.id, "digest_mismatch")
            raise ProjectRefusal("recipe_changed")
        try:
            project, canonical = await self._active_project(recipe.project_id)
            current = await self._derive(
                project, canonical, label=stored.label, script=stored.script, env=stored.env, readiness=stored.readiness,
                timeout_seconds=stored.timeout_seconds,
            )
        except ProjectRefusal as refusal:
            if refusal.code in ("project_revoked", "project_not_found"):
                raise
            await self._invalidate(recipe.id, refusal.code)
            raise ProjectRefusal("recipe_changed") from None
        if current.digest() != recipe.digest:
            await self._invalidate(recipe.id, _what_changed(stored, current))
            raise ProjectRefusal("recipe_changed")
        return recipe, current, project, canonical

    async def _invalidate(self, recipe_id: uuid.UUID, reason: str) -> None:
        async with self._engine.begin() as connection:
            await ProjectRepository(connection).invalidate_recipe(recipe_id, reason=reason)

    # ---- the per-run card ------------------------------------------------------------------------------------

    async def create_run(self, *, recipe_id: uuid.UUID) -> RunView:
        recipe, spec, project, _ = await self._current_recipe(recipe_id)
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(task_id=uuid.uuid4(), status=TaskStatus.WAITING_APPROVAL, request={"type": PROJECT_TASK_TYPE})
            await tasks.append_event(task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value})
            scope = run_scope(
                spec, task_id=task.id, run_id=uuid.uuid4(), recipe_id=recipe.id, recipe_revision=recipe.revision,
                recipe_digest=recipe.digest, project_label=project.label,
            )
            grant = await ProjectRepository(connection).insert_grant(grant_id=uuid.uuid4(), scope=scope)
            await self._event(
                connection, task.id, TaskEventType.TASK_PROJECT_RUN_REQUESTED,
                {"recipe_id": str(recipe.id), "grant_id": str(grant.id), "scope_digest": grant.scope_digest},
            )
            return await self._view(connection, task.id)

    async def confirm(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int) -> RunView:
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id)
            repository = ProjectRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise ProjectRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise ProjectRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise ProjectRefusal("grant_changed")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id, expected_revision=expected_revision, scope_digest=grant.scope_digest, ttl=self._grant_ttl
            )
            if confirmed is None:
                raise ProjectRefusal("grant_changed")
            await self._event(connection, task_id, TaskEventType.TASK_PROJECT_RUN_GRANTED, {"grant_id": str(grant.id), "grant_revision": confirmed.revision})
            await self._move(connection, task_id, TaskStatus.READY)
            return await self._view(connection, task_id)

    async def revoke(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None) -> RunView:
        """Decline, before a start. It never stops a running run (that is `stop`)."""
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id, require_accepting=False)
            repository = ProjectRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise ProjectRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                if await repository.close_grant(grant_id=grant.id, status=GrantStatus.REVOKED, expected_revision=expected_revision) is None:
                    raise ProjectRefusal("grant_changed")
                await self._event(connection, task_id, TaskEventType.TASK_PROJECT_RUN_REVOKED, {"grant_id": str(grant.id)})
            return await self._view(connection, task_id)

    # ---- start ---------------------------------------------------------------------------------------------

    async def start(self, task_id: uuid.UUID) -> RunView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            repository = ProjectRepository(connection)
            grant = await repository.grant_for_task(task_id)
            if grant is None:
                raise ProjectRefusal("grant_not_found")
            if await repository.run_for_task(task_id) is not None:
                return await self._view(connection, task_id)  # one start per approval, ever
        if grant.status is not GrantStatus.ACTIVE:
            raise ProjectRefusal("grant_not_active")
        scope = grant.scope
        recipe, spec, project, canonical = await self._current_recipe(scope.recipe_id)
        if recipe.digest != scope.recipe_digest or recipe.revision != scope.recipe_revision:
            raise ProjectRefusal("recipe_changed")
        # Read-only pre-checks, before anything is spent.
        texts = [text for text in (spec.pre_text, spec.script_text, spec.post_text) if text]
        dependencies = await asyncio.to_thread(check_dependencies, canonical, await asyncio.to_thread(package_facts, canonical), texts)
        if not dependencies.ok:
            async with self._engine.begin() as connection:
                await self._lock(connection, task_id)
                await self._event(connection, task_id, TaskEventType.TASK_PROJECT_RUN_BLOCKED, {"reason": "missing_dependency"})
            raise ProjectRefusal("missing_dependency")
        if spec.readiness.kind == "http" and spec.readiness.port is not None:
            occupied = await asyncio.to_thread(listeners, spec.readiness.port)
            if occupied:
                raise ProjectRefusal("port_in_use")
        if task_id in self._starting:
            raise ProjectRefusal("wrong_phase")
        self._starting.add(task_id)
        try:
            return await self._start(task_id, grant, scope, spec, canonical)
        finally:
            self._starting.discard(task_id)

    async def _start(self, task_id: uuid.UUID, grant: RunGrantRecord, scope: ProjectRunScope, spec: RecipeSpec, canonical: str) -> RunView:
        # 1. The run row, committed first. The partial unique index refuses a second live run of this project.
        try:
            async with self._engine.begin() as connection:
                await self._lock(connection, task_id)
                await ProjectRepository(connection).insert_run(scope=scope, grant_id=grant.id)
        except IntegrityError:
            raise ProjectRefusal("run_already_active") from None
        # 2. The attempt, with the project-run effect key, committed before any process exists.
        try:
            view, created = await self._actions.start_scoped_attempt(
                task_id, tool_name=TOOL_PROJECT_START, idempotency_key=_START_KEY, risk_tier=RiskTier.R3,
                proposal={"run_id": str(scope.run_id), "recipe_id": str(scope.recipe_id), "recipe_digest": scope.recipe_digest},
                authorizer=_RunAuthorizer(grant, self._runtime_generation),
                effect_keys=(EffectKey(key=f"project_run:{run_effect_key_value(str(scope.project_id))}", kind=EffectKind.PROJECT_RUN),),
            )
        except EffectLockedError:
            await self._end_run(scope.run_id, "FAILED", error_code="effect_locked")
            raise ProjectRefusal("effect_locked") from None
        except BaseException:
            await self._end_run(scope.run_id, "FAILED", error_code="not_started")
            raise
        if not created:
            async with self._engine.connect() as connection:
                return await self._view(connection, task_id)
        async with self._engine.begin() as connection:
            await ProjectRepository(connection).update_run(scope.run_id, action_id=view.action.id)
        # 3a. The facts once more, immediately before the spawn (S3 review finding 4): anything that moved since
        #     the start's own re-derivation ends this attempt as a clean failure, before any process exists.
        latest = await asyncio.to_thread(package_facts, canonical)
        moved = (
            (latest.package_sha256, latest.lockfile_sha256, latest.npmrc_sha256) != (spec.package_sha256, spec.lockfile_sha256, spec.npmrc_sha256)
            or await asyncio.to_thread(ancestor_npm_context, canonical) is not None
        )
        if moved:
            async def refused(connection: AsyncConnection, _: Any) -> None:
                await ProjectRepository(connection).update_run(scope.run_id, status="FAILED", error_code="recipe_changed", ended_at=datetime.now().astimezone())

            await self._actions.finish_attempt(view.action.id, outcome=AttemptOutcome.FAILED, result={"run_id": str(scope.run_id)}, error_code="recipe_changed", record=refused)
            await self._invalidate(scope.recipe_id, "changed_before_spawn")
            raise ProjectRefusal("recipe_changed")
        # 3b. Suspended -> job -> durable (pid, creation time) -> resume.
        loop = asyncio.get_running_loop()
        run_dir = os.path.join(self._run_root, str(scope.run_id))
        environment = build_environment(run_dir=run_dir, node_dir=os.path.dirname(spec.node.path), extra=spec.env)

        def record_pid(pid: int, creation_time: int) -> None:
            asyncio.run_coroutine_threadsafe(self._record_pid(scope.run_id, pid, creation_time), loop).result(timeout=30)

        outcome, error_code = AttemptOutcome.SUCCEEDED, None
        process: RunProcess | None = None
        try:
            process = await asyncio.to_thread(
                launch, LaunchSpec(
                    node_path=spec.node.path, npm_cli_path=spec.npm_cli.path, script=spec.script, cwd=canonical, env=environment,
                    redact=((run_dir, "<run>"), (canonical, "<project>"), (os.path.dirname(spec.node.path), "<node>")),
                ),
                record_pid=record_pid,
            )
        except LaunchError as error:
            outcome = AttemptOutcome.OUTCOME_UNKNOWN if error.effect_possible else AttemptOutcome.FAILED
            error_code = error.code
        except Exception:  # noqa: BLE001 - never leave the global-tier attempt EXECUTING (S3 review finding 6)
            logger.exception("project launch raised unexpectedly")
            outcome, error_code = AttemptOutcome.OUTCOME_UNKNOWN, "launch_error"
        if process is not None:
            # Supervised BEFORE the ledger write, so Stop and the timeout work even if that write fails.
            self._processes[scope.run_id] = process
            self._monitors[scope.run_id] = asyncio.create_task(self._monitor(scope.run_id, process, spec))

        async def record(connection: AsyncConnection, _: Any) -> None:
            repository = ProjectRepository(connection)
            if outcome is AttemptOutcome.SUCCEEDED:
                await repository.update_run(scope.run_id, status="RUNNING", resumed_at=datetime.now().astimezone())
                await repository.close_grant(grant_id=grant.id, status=GrantStatus.COMPLETED)
            elif outcome is AttemptOutcome.FAILED:
                await repository.update_run(scope.run_id, status="FAILED", error_code=error_code, ended_at=datetime.now().astimezone())
            else:
                await repository.update_run(scope.run_id, status="OUTCOME_UNKNOWN", error_code=error_code)

        await self._actions.finish_attempt(view.action.id, outcome=outcome, result={"run_id": str(scope.run_id)}, error_code=error_code, record=record)
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    async def _record_pid(self, run_id: uuid.UUID, pid: int, creation_time: int) -> None:
        async with self._engine.begin() as connection:
            await ProjectRepository(connection).update_run(run_id, pid=pid, creation_time=creation_time)

    async def _end_run(self, run_id: uuid.UUID, status: str, *, error_code: str | None = None, exit_code: int | None = None) -> None:
        async with self._engine.begin() as connection:
            await ProjectRepository(connection).update_run(
                run_id, only_if=("STARTING", "RUNNING", "READY", "OUTCOME_UNKNOWN"), status=status, error_code=error_code,
                exit_code=exit_code, ended_at=datetime.now().astimezone(),
            )

    # ---- supervision -----------------------------------------------------------------------------------------

    async def _monitor(self, run_id: uuid.UUID, process: RunProcess, spec: RecipeSpec) -> None:
        started = time.monotonic()
        ready = False
        try:
            while True:
                await asyncio.sleep(self._poll)
                code = process.exit_code()
                if code is not None:
                    if spec.readiness.kind == "exit_code":
                        status, error = ("SUCCEEDED", None) if code == 0 else ("FAILED", "exit_nonzero")
                    else:
                        status, error = ("SUCCEEDED", None) if code == 0 else ("FAILED", "exited")
                    await asyncio.to_thread(process.stop)  # the stop policy: nothing of the run outlives it
                    await self._end_run(run_id, status, error_code=error, exit_code=code)
                    return
                if spec.readiness.kind == "http" and not ready and spec.readiness.port is not None:
                    if await self._http_ready(process, spec.readiness.port, spec.readiness.path or "/"):
                        ready = True
                        async with self._engine.begin() as connection:
                            await ProjectRepository(connection).update_run(run_id, only_if=("RUNNING",), status="READY", ready_at=datetime.now().astimezone())
                        continue
                timed_out = time.monotonic() - started > spec.timeout_seconds
                if timed_out and (spec.readiness.kind == "exit_code" or not ready):
                    await asyncio.to_thread(process.stop)
                    await self._end_run(run_id, "FAILED", error_code="timeout")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - supervision must never leave a run unattended
            logger.exception("project run supervision failed; stopping the run")
            await asyncio.to_thread(process.stop)
            await self._end_run(run_id, "FAILED", error_code="supervision_failed")
        finally:
            self._final_logs[run_id] = tuple(process.log.tail(50))
            self._processes.pop(run_id, None)
            self._monitors.pop(run_id, None)
            process.close()

    async def _http_ready(self, process: RunProcess, port: int, path: str) -> bool:
        """Ready only if EVERY listener on the port is a member of this run's job, and it answers."""
        try:
            found = await asyncio.to_thread(listeners, port)
        except OSError:
            return False
        if not found or not all(process.job_contains(listener.pid) for listener in found):
            return False
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2.0, follow_redirects=False) as client:
                response = await client.get(f"http://127.0.0.1:{port}{path}")
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def stop(self, task_id: uuid.UUID) -> RunView:
        """Terminate THIS run's job. Never another process, never by name or bare PID."""
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            run = await ProjectRepository(connection).run_for_task(task_id)
        if run is None:
            raise ProjectRefusal("run_not_found")
        process = self._processes.get(run.id)
        if process is None or run.status not in ("RUNNING", "READY"):
            raise ProjectRefusal("wrong_phase")
        monitor = self._monitors.get(run.id)
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)  # its `finally` closes the job; wait for it
        await asyncio.to_thread(process.stop)
        await self._end_run(run.id, "STOPPED", exit_code=process.exit_code())
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id, require_accepting=False)
            await self._event(connection, task_id, TaskEventType.TASK_PROJECT_RUN_STOPPED, {"run_id": str(run.id)})
            return await self._view(connection, task_id)

    async def shutdown(self) -> None:
        """Runtime shutdown: stop every owned run (kill-on-close would, too)."""
        for run_id, process in list(self._processes.items()):
            monitor = self._monitors.get(run_id)
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            await asyncio.to_thread(process.stop)
            await self._end_run(run_id, "STOPPED", exit_code=process.exit_code())
            process.close()
        self._processes.clear()

    # ---- recovery (startup, and on request for an unknown run) ---------------------------------------------

    async def recover(self) -> int:
        """After a restart no run can still be owned: the job's only handle died with the old runtime.

        * no (pid, creation time) recorded -> the process was never resumed: no project code ran;
        * recorded and gone -> ENDED_WITH_RUNTIME;
        * recorded and somehow still alive -> OUTCOME_UNKNOWN, and the project stays locked.
        """
        async with self._engine.connect() as connection:
            live = await ProjectRepository(connection).live_runs()
        resolved = 0
        for run in live:
            if run.id in self._processes:
                continue
            await self._settle(run)
            resolved += 1
        return resolved

    async def reconcile(self, task_id: uuid.UUID) -> RunView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            run = await ProjectRepository(connection).run_for_task(task_id)
        if run is None or run.id in self._processes or run.status not in ("STARTING", "OUTCOME_UNKNOWN", "RUNNING", "READY"):
            raise ProjectRefusal("wrong_phase")
        if run.action_id is not None and (await self._actions.get_action(run.action_id)).action.status is ActionStatus.EXECUTING:
            # M10 S5 review finding 8: the start is still in progress in THIS runtime (between the attempt and the
            # launch); its evidence is not complete, and `_start` itself settles it.
            raise ProjectRefusal("wrong_phase")
        await self._settle(run)
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    async def _settle(self, run: RunRecord) -> None:
        if run.pid is None or run.creation_time is None:
            alive: bool | None = False
            status, evidence = "FAILED", {"source": "project_runs", "pid_recorded": False, "resumed": False}
        else:
            alive = await asyncio.to_thread(process_is_alive, run.pid, run.creation_time)
            status = "OUTCOME_UNKNOWN" if alive is not False else "ENDED_WITH_RUNTIME"
            evidence = {"source": "process", "pid_recorded": True, "alive": alive, "resumed": run.resumed_at is not None}
        if status == "OUTCOME_UNKNOWN":
            async with self._engine.begin() as connection:
                await ProjectRepository(connection).update_run(run.id, status="OUTCOME_UNKNOWN", error_code="alive_after_restart" if alive else "unknown")
        else:
            await self._end_run(run.id, status, error_code="never_resumed" if status == "FAILED" else None)
        if run.action_id is None:
            return
        view = await self._actions.get_action(run.action_id)
        if view.action.status not in (ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING) or status == "OUTCOME_UNKNOWN":
            return
        if view.action.status is ActionStatus.OUTCOME_UNKNOWN:
            view = await self._actions.begin_reconciliation(run.action_id, expected_revision=view.action.revision)
        # Never resumed -> no project code ran (FAILED). Recorded and gone -> it may have run, and it is over.
        result = AttemptOutcome.FAILED if status == "FAILED" else AttemptOutcome.SUCCEEDED
        await self._actions.finish_reconciliation(run.action_id, result=result, evidence=evidence, expected_revision=view.action.revision)

    # ---- reading -------------------------------------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> RunView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            return await self._view(connection, task_id)

    async def latest(self) -> RunView | None:
        async with self._engine.connect() as connection:
            row = (await connection.execute(_LATEST_RUN_TASK)).scalar_one_or_none()
            return None if row is None else await self._view(connection, row)

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> RunView:
        task = await TaskRepository(connection).get_task(task_id)
        assert task is not None
        repository = ProjectRepository(connection)
        grant = await repository.grant_for_task(task_id)
        run = await repository.run_for_task(task_id)
        action = await ActionRepository(connection).get_action_by_idempotency_key(task_id=task_id, idempotency_key=_START_KEY)
        if action is not None and action.tool_name != TOOL_PROJECT_START:  # M10 S5 review finding 4
            action = None
        process = self._processes.get(run.id) if run is not None else None
        return RunView(
            task_id=task.id, task_status=task.status.value, task_revision=task.revision, phase=_phase(grant, run, action),
            grant=grant, run=run, start_status=action.status.value if action else None,
            active_processes=process.active_processes() if process is not None else 0,
            ready=run is not None and run.status == "READY",
            log_tail=tuple(process.log.tail(50)) if process is not None else self._final_logs.get(run.id, ()) if run is not None else (),
        )

    @staticmethod
    async def _require_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != PROJECT_TASK_TYPE:
            raise TaskKindMismatchError(task_id, PROJECT_TASK_TYPE)
        return task

    @staticmethod
    async def _lock(connection: AsyncConnection, task_id: uuid.UUID, *, require_accepting: bool = True) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != PROJECT_TASK_TYPE:
            raise TaskKindMismatchError(task_id, PROJECT_TASK_TYPE)
        if require_accepting and not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task_id, task.status)
        return task

    @staticmethod
    async def _event(connection: AsyncConnection, task_id: uuid.UUID, event_type: TaskEventType, payload: dict[str, Any]) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        assert current is not None
        advanced = await tasks.advance_task(task_id=current.id, expected_revision=current.revision)
        if advanced is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task_id)
        await tasks.append_event(task=advanced, event_type=event_type, payload=payload)

    @staticmethod
    async def _move(connection: AsyncConnection, task_id: uuid.UUID, status: TaskStatus) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        if current is None or not accepts_actions(current.status) or current.status is status:
            return
        await tasks.advance_task(task_id=current.id, expected_revision=current.revision, status=status)


def _what_changed(stored: RecipeSpec, current: RecipeSpec) -> str:
    if (stored.project_volume, stored.project_index) != (current.project_volume, current.project_index):
        return "project_changed"
    if stored.node != current.node or stored.npm_cli != current.npm_cli:
        return "executable_changed"
    if stored.script_text != current.script_text or stored.pre_text != current.pre_text or stored.post_text != current.post_text:
        return "script_changed"
    if stored.npmrc_sha256 != current.npmrc_sha256:
        return "npmrc_changed"
    if stored.lockfile_sha256 != current.lockfile_sha256 or stored.lockfile_name != current.lockfile_name:
        return "lockfile_changed"
    if stored.package_sha256 != current.package_sha256:
        return "package_json_changed"
    return "recipe_changed"


def _phase(grant: RunGrantRecord | None, run: RunRecord | None, action: ActionRecord | None) -> str:
    if run is not None:
        return {
            "STARTING": "starting", "RUNNING": "running", "READY": "ready", "SUCCEEDED": "succeeded", "FAILED": "failed",
            "STOPPED": "stopped", "ENDED_WITH_RUNTIME": "ended_with_runtime", "OUTCOME_UNKNOWN": "outcome_unknown",
        }[run.status]
    if grant is None:
        return "declined"
    if grant.status is GrantStatus.PENDING:
        return "awaiting_approval"
    if grant.status is GrantStatus.ACTIVE:
        return "approved"
    if grant.status is GrantStatus.EXPIRED:
        return "expired"
    return "declined"


_LATEST_RUN_TASK = (
    select(tasks_table.c.id)
    .where(tasks_table.c.request["type"].astext == PROJECT_TASK_TYPE)
    .order_by(tasks_table.c.created_at.desc(), tasks_table.c.id)
    .limit(1)
)
