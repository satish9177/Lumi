import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.task_status import TaskStatus
from app.services.tasks import TaskService


async def test_concurrent_cancellations_write_exactly_one_transition(engine: AsyncEngine) -> None:
    service = TaskService(engine)
    task = await service.create_task({"type": "appointment_search"})

    results = await asyncio.gather(*(service.cancel_task(task.id) for _ in range(8)))

    assert {(r.status, r.revision) for r in results} == {(TaskStatus.CANCELLED, 2)}
    events = await service.list_events(task.id)
    assert [(e.sequence, e.event_type) for e in events] == [
        (1, "task.created"),
        (2, "task.cancelled"),
    ]


async def test_concurrent_conditioned_cancellations_have_one_winner(engine: AsyncEngine) -> None:
    service = TaskService(engine)
    task = await service.create_task({"type": "appointment_search"})

    results = await asyncio.gather(
        *(service.cancel_task(task.id, expected_revision=1) for _ in range(8)),
        return_exceptions=True,
    )

    # Losers either observe the stale revision or, having read revision 1 before
    # the winner committed, retry and then observe it. None writes a second event.
    assert all(not isinstance(r, BaseException) or type(r).__name__ == "StaleTaskRevisionError" for r in results)
    assert any(not isinstance(r, BaseException) for r in results)
    assert len(await service.list_events(task.id)) == 2
