import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import task_events, tasks
from app.domain.task_status import TaskStatus
from app.repositories.tasks import TaskRepository

APPOINTMENT = {
    "type": "appointment_search",
    "text": "Find a dermatologist appointment Saturday evening",
}


async def _create(client: httpx.AsyncClient, request: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await client.post("/tasks", json={"request": request or APPOINTMENT})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _force_status(engine: AsyncEngine, task_id: str, status: TaskStatus) -> None:
    """Stand-in for future executor transitions, which have no API yet."""
    async with engine.begin() as connection:
        repository = TaskRepository(connection)
        task = await repository.get_task(uuid.UUID(task_id))
        assert task is not None
        assert await repository.update_status(
            task_id=task.id, expected_revision=task.revision, status=status
        )


async def test_health_reports_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_create_task_persists_row_and_initial_event(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    task = await _create(client)

    assert uuid.UUID(task["id"]).version == 4
    assert task["status"] == "CREATED"
    assert task["revision"] == 1
    assert task["request"] == APPOINTMENT
    assert task["created_at"] == task["updated_at"]

    # Read back through a separate connection, not the API.
    async with engine.connect() as connection:
        row = (await connection.execute(select(tasks).where(tasks.c.id == uuid.UUID(task["id"])))).one()
        events = (
            await connection.execute(
                select(task_events).where(task_events.c.task_id == uuid.UUID(task["id"]))
            )
        ).all()
    assert row.status == "CREATED"
    assert row.request == APPOINTMENT
    assert [(e.sequence, e.task_revision, e.event_type, e.payload) for e in events] == [
        (1, 1, "task.created", {"status": "CREATED"})
    ]


async def test_create_keeps_structured_extra_fields(client: httpx.AsyncClient) -> None:
    request = {"type": "appointment_search", "constraints": {"day": "saturday", "after": "17:00"}}
    task = await _create(client, request)
    assert task["request"] == request


async def test_get_task_returns_persisted_task(client: httpx.AsyncClient) -> None:
    created = await _create(client)
    response = await client.get(f"/tasks/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


async def test_events_come_back_in_sequence_order(client: httpx.AsyncClient) -> None:
    created = await _create(client)
    await _create(client)  # Another task's events must not interleave.
    await client.post(f"/tasks/{created['id']}/cancel")

    response = await client.get(f"/tasks/{created['id']}/events")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == created["id"]
    assert [(e["sequence"], e["event_type"], e["task_revision"]) for e in body["events"]] == [
        (1, "task.created", 1),
        (2, "task.cancelled", 2),
    ]
    assert {e["task_id"] for e in body["events"]} == {created["id"]}

    after_first = await client.get(f"/tasks/{created['id']}/events", params={"after_sequence": 1})
    assert [e["sequence"] for e in after_first.json()["events"]] == [2]


async def test_cancel_updates_task_and_records_event(client: httpx.AsyncClient) -> None:
    created = await _create(client)

    response = await client.post(f"/tasks/{created['id']}/cancel")

    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["id"] == created["id"]
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["revision"] == 2
    assert cancelled["created_at"] == created["created_at"]
    assert cancelled["updated_at"] >= created["updated_at"]
    assert (await client.get(f"/tasks/{created['id']}")).json() == cancelled

    events = (await client.get(f"/tasks/{created['id']}/events")).json()["events"]
    assert events[-1]["payload"] == {"from_status": "CREATED", "to_status": "CANCELLED"}


async def test_cancelling_twice_is_idempotent(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    created = await _create(client)
    first = (await client.post(f"/tasks/{created['id']}/cancel")).json()

    second = await client.post(f"/tasks/{created['id']}/cancel")

    assert second.status_code == 200
    assert second.json() == first  # Same revision and updated_at: nothing was written.
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count()).where(task_events.c.task_id == uuid.UUID(created["id"]))
        )
    assert count == 2


@pytest.mark.parametrize("status", [TaskStatus.SUCCEEDED, TaskStatus.FAILED])
async def test_finished_tasks_cannot_be_cancelled(
    client: httpx.AsyncClient, engine: AsyncEngine, status: TaskStatus
) -> None:
    created = await _create(client)
    await _force_status(engine, created["id"], status)

    response = await client.post(f"/tasks/{created['id']}/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_not_cancellable"
    task = (await client.get(f"/tasks/{created['id']}")).json()
    assert (task["status"], task["revision"]) == (status.value, 2)


@pytest.mark.parametrize(
    "status", [TaskStatus.PLANNING, TaskStatus.READY, TaskStatus.EXECUTING, TaskStatus.VERIFYING, TaskStatus.PAUSED]
)
async def test_every_non_terminal_state_is_cancellable(
    client: httpx.AsyncClient, engine: AsyncEngine, status: TaskStatus
) -> None:
    created = await _create(client)
    await _force_status(engine, created["id"], status)

    response = await client.post(f"/tasks/{created['id']}/cancel")

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["revision"]) == ("CANCELLED", 3)
    events = (await client.get(f"/tasks/{created['id']}/events")).json()["events"]
    assert events[-1]["payload"] == {"from_status": status.value, "to_status": "CANCELLED"}


async def test_cancel_with_stale_revision_is_rejected_without_writing(client: httpx.AsyncClient) -> None:
    created = await _create(client)

    stale = await client.post(f"/tasks/{created['id']}/cancel", json={"expected_revision": 7})

    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "stale_revision",
        "message": f"Task {created['id']} is at revision 1, not 7.",
        "current_revision": 1,
    }
    assert (await client.get(f"/tasks/{created['id']}")).json() == created

    current = await client.post(f"/tasks/{created['id']}/cancel", json={"expected_revision": 1})
    assert (current.status_code, current.json()["revision"]) == (200, 2)


async def test_unknown_task_ids_return_404(client: httpx.AsyncClient) -> None:
    missing = uuid.uuid4()
    for method, path in [
        ("GET", f"/tasks/{missing}"),
        ("GET", f"/tasks/{missing}/events"),
        ("POST", f"/tasks/{missing}/cancel"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "task_not_found"


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "123", "00000000-0000-0000-0000-00000000000Z"])
async def test_malformed_task_ids_return_422(client: httpx.AsyncClient, bad_id: str) -> None:
    for method, path in [
        ("GET", f"/tasks/{bad_id}"),
        ("GET", f"/tasks/{bad_id}/events"),
        ("POST", f"/tasks/{bad_id}/cancel"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 422, path


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"request": "find a dermatologist"},
        {"request": {"text": "missing type"}},
        {"request": {"type": "Not Valid!"}},
        {"request": APPOINTMENT, "status": "SUCCEEDED"},
        {"request": {"type": "appointment_search", "notes": "x" * 70_000}},
    ],
)
async def test_invalid_create_bodies_are_rejected(
    client: httpx.AsyncClient, engine: AsyncEngine, body: dict[str, Any]
) -> None:
    response = await client.post("/tasks", json=body)
    assert response.status_code == 422
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(tasks)) == 0
