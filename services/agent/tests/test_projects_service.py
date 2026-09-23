"""Milestone 10 S3: registered projects, recipes and supervised runs, against real PostgreSQL, a real
NTFS folder and the real `node.exe` + `npm-cli.js` under Program Files. The projects are synthetic."""

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.projects import ProjectRefusal
from app.projects.process import listeners, process_is_alive
from app.services.actions import ActionService
from app.services.documents import DocumentService
from app.services.projects import ProjectService, RunView
from app.services.recovery import RecoveryService
from app.services.runtime import RuntimeGeneration
from tests.project_fixtures import free_port, make_project

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="M10 project runs ship on Windows")

PLANTED = {
    "OPENAI_API_KEY": "sk-planted-fake-openai-key-000000",
    "GEMINI_API_KEY": "AIzaPlantedFakeGeminiKey0000000000000",
    "LUMI_RUNTIME_TOKEN": "planted-lumi-runtime-token",
    "DATABASE_URL": "postgresql://planted:planted@localhost/planted",
    "GITHUB_TOKEN": "ghp_plantedfaketoken000000",
    "AWS_SECRET_ACCESS_KEY": "planted-aws-secret",
    "NPM_TOKEN": "planted-npm-token",
    "NODE_OPTIONS": "--require ./evil.js",
}


@pytest.fixture
def service(engine: AsyncEngine, action_service: ActionService, runtime_generation: RuntimeGeneration, tmp_path: Path) -> ProjectService:
    return ProjectService(
        engine, actions=action_service, runtime_generation=runtime_generation.id, grant_ttl_seconds=600,
        protected_folders=((), ()), run_root=str(tmp_path / "runs"), poll_seconds=0.1,
    )


async def _project(service: ProjectService, folder: Path, **kwargs: Any) -> uuid.UUID:
    make_project(folder, **kwargs)
    return (await service.register_project(path=str(folder), label="Synthetic Lumi")).id


async def _recipe(service: ProjectService, project_id: uuid.UUID, script: str, *, port: int | None = None, timeout: int = 30,
                  env: dict[str, str] | None = None) -> uuid.UUID:
    view = await service.create_recipe(
        project_id=project_id, label=f"Run {script}", script=script, readiness_kind="http" if port else "exit_code",
        ready_port=port, ready_path="/health" if port else None, timeout_seconds=timeout, env=env or {},
    )
    return view.id


async def _approved_run(service: ProjectService, recipe_id: uuid.UUID) -> uuid.UUID:
    view = await service.create_run(recipe_id=recipe_id)
    assert view.phase == "awaiting_approval" and view.grant is not None
    assert view.grant.scope.warning == "This recipe executes code from this project with your user-level permissions."
    view = await service.confirm(view.task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)
    assert view.phase == "approved"
    return view.task_id


async def _until(service: ProjectService, task_id: uuid.UUID, phases: set[str], timeout: float = 30) -> RunView:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        view = await service.describe(task_id)
        if view.phase in phases:
            return view
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"stuck in {view.phase} ({view.run}): {view.log_tail}")
        await asyncio.sleep(0.1)


def _running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259
    finally:
        kernel32.CloseHandle(handle)


async def _code(awaitable: Any) -> str:
    with pytest.raises(ProjectRefusal) as refused:
        await awaitable
    return refused.value.code


# ---- a run, end to end --------------------------------------------------------------------------------------


async def test_an_http_recipe_runs_in_its_own_job_and_is_ready_only_when_its_job_owns_the_port(
    service: ProjectService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    port = free_port()
    folder = tmp_path / "lumi-app"
    project = await _project(service, folder, port=port)
    recipe = await _recipe(service, project, "serve", port=port, env={"APP_MODE": "demo"})
    task = await _approved_run(service, recipe)
    view = await service.start(task)
    assert view.start_status == "SUCCEEDED" and view.run is not None and view.run.pid is not None
    view = await _until(service, task, {"ready"})
    assert view.ready and view.active_processes >= 2
    assert any("listening on" in line for line in view.log_tail)
    assert not any("\x1b" in line for line in view.log_tail), "the log is inert text"

    # The planted secrets never reached the child; the recipe's own variable did.
    dumped = json.loads((folder / "env-dump.json").read_text(encoding="utf-8"))
    for name, value in PLANTED.items():
        assert name not in dumped and value not in json.dumps(dumped), name
    assert dumped.get("APP_MODE") == "demo"
    assert not any(key.upper().startswith("LUMI_") for key in dumped)

    # Stop ends the whole tree (the grandchild too), and only it.
    grandchild = int((folder / "grandchild.pid").read_text(encoding="utf-8"))
    view = await service.stop(task)
    assert view.phase == "stopped"
    await asyncio.sleep(0.5)
    assert not listeners(port)
    assert not _running(grandchild)


async def test_an_exit_code_recipe_succeeds_or_fails_by_its_exit_code(service: ProjectService, tmp_path: Path) -> None:
    project = await _project(service, tmp_path / "p")
    ok = await _approved_run(service, await _recipe(service, project, "check"))
    await service.start(ok)
    assert (await _until(service, ok, {"succeeded", "failed"})).phase == "succeeded"
    bad = await _approved_run(service, await _recipe(service, project, "fail"))
    await service.start(bad)
    view = await _until(service, bad, {"succeeded", "failed"})
    assert view.phase == "failed" and view.run is not None and view.run.exit_code == 3


async def test_a_run_that_overruns_its_timeout_is_ended_by_its_job(service: ProjectService, tmp_path: Path) -> None:
    project = await _project(service, tmp_path / "p")
    task = await _approved_run(service, await _recipe(service, project, "hang", timeout=5))
    await service.start(task)
    view = await _until(service, task, {"failed"}, timeout=20)
    assert view.run is not None and view.run.error_code == "timeout"


# ---- one start per approval, one live run per project ---------------------------------------------------------


async def test_no_duplicate_launch(service: ProjectService, tmp_path: Path) -> None:
    port = free_port()
    project = await _project(service, tmp_path / "p", port=port)
    recipe = await _recipe(service, project, "serve", port=port)
    first = await _approved_run(service, recipe)
    await service.start(first)
    await _until(service, first, {"ready"})
    # The same approval never starts twice...
    again = await service.start(first)
    assert again.run is not None and again.phase == "ready"
    # ...and a second approval of the same project is refused while the first run is live.
    second = await _approved_run(service, recipe)
    assert await _code(service.start(second)) in ("port_in_use", "run_already_active")
    other = await _approved_run(service, await _recipe(service, project, "check"))
    assert await _code(service.start(other)) == "run_already_active"
    await service.stop(first)


async def test_nothing_runs_without_the_trusted_click(service: ProjectService, tmp_path: Path) -> None:
    project = await _project(service, tmp_path / "p")
    view = await service.create_run(recipe_id=await _recipe(service, project, "check"))
    assert await _code(service.start(view.task_id)) == "grant_not_active"


# ---- any change invalidates the recipe --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda folder: (folder / "package-lock.json").write_text('{"changed": true}', encoding="utf-8"), "lockfile_changed"),
        (lambda folder: (folder / "package.json").write_text(
            (folder / "package.json").read_text(encoding="utf-8").replace("node exit.js 0", "node exit.js 0 && calc"),
            encoding="utf-8"), "script_changed"),
        (lambda folder: (folder / "package.json").write_text(
            (folder / "package.json").read_text(encoding="utf-8").replace('"1.0.0"', '"1.0.1"'), encoding="utf-8"),
         "package_json_changed"),
        (lambda folder: (folder / ".npmrc").write_text("script-shell=powershell.exe\n", encoding="utf-8"), "project_npmrc_refused"),
    ],
)
async def test_any_change_invalidates_the_recipe(service: ProjectService, tmp_path: Path, change: Any, reason: str) -> None:
    folder = tmp_path / "p"
    project = await _project(service, folder)
    recipe = await _recipe(service, project, "check")
    task = await _approved_run(service, recipe)
    change(folder)
    assert await _code(service.start(task)) == "recipe_changed"
    recipes = await service.list_recipes(project)
    assert [(r.status, r.invalid_reason) for r in recipes if r.id == recipe] == [("INVALIDATED", reason)]
    # It stays invalid even if the change is undone: re-registration is a trusted act.
    assert await _code(service.create_run(recipe_id=recipe)) == "recipe_changed"


async def test_a_changed_node_install_invalidates_the_recipe(service: ProjectService, tmp_path: Path, engine: AsyncEngine) -> None:
    project = await _project(service, tmp_path / "p")
    recipe = await _recipe(service, project, "check")
    task = await _approved_run(service, recipe)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE project_recipes SET spec = jsonb_set(spec, '{node,sha256}', to_jsonb(repeat('0', 64))) WHERE id = :r"), {"r": recipe}
        )
    # The stored spec no longer matches its digest: tampering with the row is refused too.
    assert await _code(service.start(task)) == "recipe_changed"


# ---- dependencies: BLOCKED, never installed -------------------------------------------------------------------


async def test_a_missing_dependency_blocks_the_run_and_installs_nothing(service: ProjectService, tmp_path: Path) -> None:
    folder = tmp_path / "p"
    project = await _project(service, folder, dependencies=True, install=False)
    task = await _approved_run(service, await _recipe(service, project, "tool"))
    assert await _code(service.start(task)) == "missing_dependency"
    assert not (folder / "node_modules").exists(), "Lumi never installs"
    view = await service.describe(task)
    assert view.run is None and view.phase == "approved", "the approval is not spent by a blocked start"


# ---- the environment policy -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"OPENAI_API_KEY": "x"}, {"MY_TOKEN": "x"}, {"DB_PASSWORD": "x"}, {"LUMI_MODE": "x"}, {"NODE_OPTIONS": "--require x"},
        {"PATH": "C:\\evil"}, {"ComSpec": "evil.exe"}, {"SAFE": "sk-abcdefghijklmnop"}, {"NPM_CONFIG_REGISTRY": "http://evil"},
        {"GIT_ASKPASS": "x"}, {"bad name": "x"}, {"OK": "line\nbreak"},
    ],
)
async def test_secret_or_code_changing_environment_is_refused(service: ProjectService, tmp_path: Path, env: dict[str, str]) -> None:
    project = await _project(service, tmp_path / "p")
    with pytest.raises(ProjectRefusal):
        await _recipe(service, project, "check", env=env)


# ---- registration ----------------------------------------------------------------------------------------------


async def test_a_recipe_can_only_name_a_declared_script(service: ProjectService, tmp_path: Path) -> None:
    project = await _project(service, tmp_path / "p")
    for script in ("install", "not-declared", "serve && calc", "../x"):
        with pytest.raises(ProjectRefusal):
            await _recipe(service, project, script)


async def test_a_download_folder_can_never_be_a_project(service: ProjectService, engine: AsyncEngine, tmp_path: Path) -> None:
    documents = DocumentService(engine, grant_ttl_seconds=600, protected_folders=((), ()))
    folder = tmp_path / "shared"
    make_project(folder)
    await documents.register_root(path=str(tmp_path), label="Downloads", can_read=False, can_create=True, can_modify=False)
    assert await _code(service.register_project(path=str(folder), label="Nope")) == "overlaps_download_folder"


async def test_a_non_node_folder_is_not_a_project(service: ProjectService, tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    assert await _code(service.register_project(path=str(folder), label="Empty")) == "package_json_missing"


# ---- restart ------------------------------------------------------------------------------------------------------


async def test_a_run_never_resumed_before_a_crash_is_authoritatively_not_run(
    service: ProjectService, tmp_path: Path, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.projects as projects_module

    project = await _project(service, tmp_path / "p")
    task = await _approved_run(service, await _recipe(service, project, "check"))

    def crash(*_: Any, **__: Any) -> Any:
        raise KeyboardInterrupt  # the runtime dies before the process was created

    monkeypatch.setattr(projects_module, "launch", crash)
    with pytest.raises(KeyboardInterrupt):
        await service.start(task)
    await RecoveryService(engine).recover_unfinished_attempts(uuid.uuid4())
    assert (await service.describe(task)).start_status == "OUTCOME_UNKNOWN"
    assert await service.recover() == 1
    view = await service.describe(task)
    assert view.phase == "failed" and view.run is not None and view.run.error_code == "never_resumed"
    assert view.start_status == "FAILED"


async def test_a_run_whose_process_is_gone_after_a_restart_ended_with_the_runtime(
    service: ProjectService, tmp_path: Path, engine: AsyncEngine, action_service: ActionService, runtime_generation: RuntimeGeneration,
) -> None:
    project = await _project(service, tmp_path / "p")
    task = await _approved_run(service, await _recipe(service, project, "hang", timeout=60))
    view = await service.start(task)
    assert view.run is not None and view.run.pid is not None
    pid, created = view.run.pid, view.run.creation_time
    assert created is not None and process_is_alive(pid, created) is True
    # The runtime "dies": its job handle closes (kill-on-close) and a fresh service starts.
    await service.shutdown()
    assert process_is_alive(pid, created) is False
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE project_runs SET status = 'RUNNING', ended_at = NULL, exit_code = NULL"))
    fresh = ProjectService(engine, actions=action_service, runtime_generation=runtime_generation.id, grant_ttl_seconds=600,
                           protected_folders=((), ()), run_root=str(tmp_path / "runs"))
    assert await fresh.recover() == 1
    assert (await fresh.describe(task)).phase == "ended_with_runtime"
    # The project is free again for a NEW approval.
    again = await _approved_run(fresh, await _recipe(fresh, project, "check"))
    await fresh.start(again)
    assert (await _until(fresh, again, {"succeeded", "failed"})).phase == "succeeded"


async def test_the_generic_action_routes_can_never_start_a_project(client: Any, service: ProjectService, tmp_path: Path) -> None:
    project = await _project(service, tmp_path / "p")
    task = await _approved_run(service, await _recipe(service, project, "check"))
    for tool in ("project_start", "Project_Start"):
        planted = await client.post(
            f"/tasks/{task}/actions", json={"idempotency_key": "project-start", "tool_name": tool, "risk_tier": "R3", "proposal": {}}
        )
        assert planted.status_code in (409, 422), (tool, planted.status_code)
    assert (await service.describe(task)).start_status is None


async def test_a_project_npmrc_is_refused_because_it_could_change_what_runs(service: ProjectService, tmp_path: Path) -> None:
    """npm reads the project's own .npmrc, and an empty environment override does not neutralise its
    `node-options` (proven while building S3). So a project with a .npmrc gets no recipe at all."""
    folder = tmp_path / "p"
    make_project(folder)
    (folder / ".npmrc").write_text("node-options=--require ./evil.js\n", encoding="utf-8")
    project = (await service.register_project(path=str(folder), label="With npmrc")).id
    assert await _code(_recipe(service, project, "check")) == "project_npmrc_refused"


# ---- S3 adversarial review regressions -------------------------------------------------------------------------


async def test_a_project_inside_an_npm_workspace_or_below_node_modules_is_refused(service: ProjectService, tmp_path: Path) -> None:
    """Review findings 1 and 9: npm walks UP from the project; a parent workspace's .npmrc or `workspace=`
    (which can run ANOTHER package's script) and a parent node_modules/.bin are not part of any recipe."""
    parent = tmp_path / "mono"
    parent.mkdir()
    (parent / "package.json").write_text(json.dumps({"name": "root", "workspaces": ["child"]}), encoding="utf-8")
    project = await _project(service, parent / "child")
    assert await _code(_recipe(service, project, "check")) == "ancestor_npm_context"
    other = tmp_path / "plain"
    (other / "node_modules").mkdir(parents=True)
    project = await _project(service, other / "app")
    assert await _code(_recipe(service, project, "check")) == "ancestor_npm_context"


async def test_a_workspace_parent_appearing_after_approval_is_caught_before_the_spawn(service: ProjectService, tmp_path: Path) -> None:
    folder = tmp_path / "p" / "app"
    project = await _project(service, folder)
    task = await _approved_run(service, await _recipe(service, project, "check"))
    (tmp_path / "p" / "package.json").write_text(json.dumps({"workspaces": ["app"]}), encoding="utf-8")
    assert await _code(service.start(task)) == "recipe_changed"
    assert (await service.describe(task)).run is None


@pytest.mark.parametrize(
    "scripts",
    [
        {"pre" + "d" * 62: "node exit.js 0", "d" * 62: "node exit.js 0"},  # hook name too long to display
        {"precheck": "node exit.js 0 " + "x" * 2100},  # hook text too long to display
        {"precheck": "node exit.js 0\u202e evil"},  # a bidi override in the hook
    ],
)
async def test_a_hook_that_cannot_be_shown_refuses_the_recipe(service: ProjectService, tmp_path: Path, scripts: dict[str, str]) -> None:
    """Review finding 2: npm runs pre/post hooks; one the approval cannot show must never run unseen."""
    project = await _project(service, tmp_path / "p", extra_scripts=scripts)
    target = next(name for name in scripts if not name.startswith("pre")) if any(not n.startswith("pre") for n in scripts) else "check"
    with pytest.raises(ProjectRefusal) as refused:
        await service.create_recipe(
            project_id=project, label="Hooked", script=target, readiness_kind="exit_code", ready_port=None, ready_path=None,
            timeout_seconds=30, env={},
        )
    assert refused.value.code == "hook_not_displayable"


async def test_a_recipe_binds_the_text_the_person_confirmed(service: ProjectService, tmp_path: Path) -> None:
    """Review finding 3: package.json changed while the native confirmation was open."""
    project = await _project(service, tmp_path / "p")
    with pytest.raises(ProjectRefusal) as refused:
        await service.create_recipe(
            project_id=project, label="x", script="check", readiness_kind="exit_code", ready_port=None, ready_path=None,
            timeout_seconds=30, env={}, confirmed_texts=("node exit.js 0 && something-else", None, None),
        )
    assert refused.value.code == "script_changed_during_review"
    view = await service.create_recipe(
        project_id=project, label="x", script="check", readiness_kind="exit_code", ready_port=None, ready_path=None,
        timeout_seconds=30, env={}, confirmed_texts=("node exit.js 0", None, None),
    )
    assert view.status == "ACTIVE"


async def test_a_package_json_swapped_after_the_start_checks_never_runs(
    service: ProjectService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 4: the facts are re-read immediately before the spawn."""
    folder = tmp_path / "p"
    project = await _project(service, folder)
    task = await _approved_run(service, await _recipe(service, project, "check"))
    real = service._actions.start_scoped_attempt

    async def swap_then_start(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        text = (folder / "package.json").read_text(encoding="utf-8").replace("node exit.js 0", "node exit.js 0 && calc")
        (folder / "package.json").write_text(text, encoding="utf-8")
        return result

    monkeypatch.setattr(service._actions, "start_scoped_attempt", swap_then_start)
    assert await _code(service.start(task)) == "recipe_changed"
    view = await service.describe(task)
    assert view.run is not None and view.run.pid is None and view.run.status == "FAILED" and view.start_status == "FAILED"


async def test_an_unexpected_launch_error_never_wedges_the_global_lock(
    service: ProjectService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 6: the attempt is settled, and in-process reconciliation resolves it."""
    import app.services.projects as projects_module

    project = await _project(service, tmp_path / "p")
    task = await _approved_run(service, await _recipe(service, project, "check"))

    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("database went away while recording the pid")

    monkeypatch.setattr(projects_module, "launch", broken)
    await service.start(task)
    assert (await service.describe(task)).start_status == "OUTCOME_UNKNOWN"
    view = await service.reconcile(task)
    assert view.phase == "failed" and view.start_status == "FAILED"


def test_the_log_is_bounded_redacted_and_free_of_invisible_characters() -> None:
    """Review findings 5 and 10."""
    from app.projects.process import LOG_MAX_LINE_CHARS, BoundedLog

    log = BoundedLog(((r"C:\Users\me\AppData\Local\Lumi\project-runs\r1", "<run>"), (r"C:\Users\me\app", "<project>")))
    log.add(b"npm error log at C:\\Users\\ME\\AppData\\Local\\Lumi\\project-runs\\r1\\npm-cache\\x.log in c:\\users\\me\\app\\x\n")
    log.add("safe \u202eevil\u200b text\n".encode("utf-8"))
    log.add(b"x" * 100_000)
    first, second, third = log.tail(3)
    assert first == "npm error log at <run>\\npm-cache\\x.log in <project>\\x"
    assert second == "safe evil text"
    assert len(third) == LOG_MAX_LINE_CHARS


async def test_a_line_without_newlines_cannot_exhaust_memory(service: ProjectService, tmp_path: Path) -> None:
    """Review finding 5: a child writing a huge line with no newline is read in bounded pieces."""
    folder = tmp_path / "p"
    project = await _project(service, folder, extra_scripts={"spam": "node spam.js"})
    (folder / "spam.js").write_text("process.stdout.write('\\r'.repeat(1) + 'y'.repeat(5 * 1024 * 1024))\n", encoding="utf-8")
    task = await _approved_run(service, await _recipe(service, project, "spam"))
    await service.start(task)
    view = await _until(service, task, {"succeeded", "failed"})
    assert all(len(line) <= 400 for line in view.log_tail)
