"""Milestone 10 S3, Acceptance C: "Open VS Code and start Lumi".

Three separate effects, each with its own authority, and none funds another:

1. **Open the editor** -- M9 S3's registered-application launch, unchanged. On this machine VS Code is a
   per-user install (under %LOCALAPPDATA%\\Programs), which M9's registry correctly REFUSES: a per-user
   directory is user-writable, so its executable could be swapped. That rule is not weakened here. The
   launch half is exercised on the registered-launch path with the fake desktop worker and a stand-in
   registered application under Program Files.
2. **The approved project** -- registered through the (native, in main) folder dialog plus the execution
   warning; here the synthetic "Lumi" project.
3. **The exact recipe run** -- `npm run dev` of that project, from a recipe registered in trusted UI and a
   per-run approval, supervised in its own Job Object and ready only when its job owns the port.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.registry import AppRegistry, RegisteredApp
from app.domain.action_status import ActionStatus
from app.domain.projects import ProjectRefusal
from app.services.actions import ActionService
from app.services.desktop import DesktopService
from app.services.desktop_actions import DesktopActionService
from app.services.projects import ProjectService
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from tests.project_fixtures import free_port, make_project
from tests.test_desktop_actions_service import FakeEffectDesktop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="M10 ships on Windows")

EDITOR = RegisteredApp(app_id="editor", label="Code editor (stand-in)", executable="C:\\Program Files\\Editor\\editor.exe")


def test_the_real_per_user_vs_code_is_refused_by_the_registry_and_the_rule_is_not_weakened() -> None:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    code = str(Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe")
    with pytest.raises(ValueError):
        AppRegistry.from_config(json.dumps([{"app_id": "vscode", "label": "VS Code", "executable": code}]))


async def test_open_the_editor_and_start_the_project_are_separate_effects_with_separate_approvals(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: RuntimeGeneration, tmp_path: Path
) -> None:
    desktop = FakeEffectDesktop(engine, runtime_generation.id)
    launcher = DesktopActionService(
        engine, actions=action_service, tasks=task_service, desktop=cast(DesktopService, desktop), registry=AppRegistry([EDITOR])
    )
    projects = ProjectService(
        engine, actions=action_service, runtime_generation=runtime_generation.id, grant_ttl_seconds=600,
        protected_folders=((), ()), run_root=str(tmp_path / "runs"), poll_seconds=0.1,
    )
    port = free_port()
    lumi = make_project(tmp_path / "Lumi", port=port, extra_scripts={"dev": f"node server.js {port}"})

    # ---- effect 1: open the editor (M9 S3 registered launch, its own exact approval) ----
    launch = await launcher.propose_launch(app_id="editor")
    # Nothing has launched and nothing has run before its own approval.
    assert desktop.requests == []

    # ---- effect 2: the approved project, and a recipe registered in trusted UI ----
    project = await projects.register_project(path=str(lumi), label="Lumi")
    recipe = await projects.create_recipe(
        project_id=project.id, label="Start Lumi", script="dev", readiness_kind="http", ready_port=port, ready_path="/health",
        timeout_seconds=60, env={},
    )

    # ---- effect 3: the exact recipe run, its own per-run approval ----
    card = await projects.create_run(recipe_id=recipe.id)
    assert card.grant is not None
    assert card.grant.scope.argv == ("node.exe", "npm-cli.js", "run", "dev")

    # Approving the launch does not start the project...
    launched = await launcher.approve(launch.action_id, expected_revision=launch.revision)
    assert launched.status is ActionStatus.SUCCEEDED and len(desktop.requests) == 1
    with pytest.raises(ProjectRefusal) as refused:
        await projects.start(card.task_id)
    assert refused.value.code == "grant_not_active"
    # ...and approving the run is a different grant, in a different task, that could never launch anything.
    granted = await projects.confirm(card.task_id, grant_id=card.grant.id, expected_revision=card.grant.revision)
    started = await projects.start(granted.task_id)
    assert started.start_status == "SUCCEEDED"
    deadline = asyncio.get_running_loop().time() + 30
    while (view := await projects.describe(card.task_id)).phase != "ready":
        assert asyncio.get_running_loop().time() < deadline, view
        await asyncio.sleep(0.1)
    assert len(desktop.requests) == 1, "the project run did not touch the desktop"

    async with engine.connect() as connection:
        rows = (await connection.execute(text("SELECT task_id, tool_name, status FROM actions ORDER BY created_at"))).all()
        kinds = (await connection.execute(text("SELECT task_id, kind FROM task_grants"))).all()
    tools = {row.tool_name for row in rows}
    assert {"project_start"} <= tools and len({row.task_id for row in rows}) == 2, rows
    assert [kind for _, kind in kinds] == ["project_run"], "the launch was an exact approval, the run a per-run grant"
    await projects.stop(card.task_id)
