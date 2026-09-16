import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.repositories.actions import ActionRepository

_RUNTIME_ADVISORY_LOCK = 5_500_381_228_079_332_660


class RuntimeAlreadyActiveError(RuntimeError):
    pass


async def _monitor_lease(
    connection: AsyncConnection,
    *,
    interval_seconds: float,
    on_lost: Callable[[int], object],
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            # This transaction owns the advisory lock. The local process mutex
            # prevents another app.server from starting during this detection
            # window; this monitor makes a lost database session fail closed.
            await connection.execute(text("SELECT 1"))
        except asyncio.CancelledError:
            raise
        except Exception:
            on_lost(70)
            return


@asynccontextmanager
async def runtime_ownership(
    engine: AsyncEngine,
    *,
    monitor_interval_seconds: float = 1.0,
    on_lost: Callable[[int], object] = os._exit,
) -> AsyncIterator[None]:
    """Hold exclusive database ownership before recovery and until shutdown."""

    connection = await engine.connect()
    transaction = await connection.begin()
    monitor: asyncio.Task[None] | None = None
    try:
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _RUNTIME_ADVISORY_LOCK}
        )
        if acquired is not True:
            raise RuntimeAlreadyActiveError("another Lumi agent runtime is already active")
        monitor = asyncio.create_task(
            _monitor_lease(
                connection,
                interval_seconds=monitor_interval_seconds,
                on_lost=on_lost,
            )
        )
        yield
    finally:
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()


@dataclass(frozen=True, slots=True)
class RuntimeGeneration:
    """One run of the runtime process.

    Every execution attempt records the generation that started it. That is what
    lets startup separate "work this process is doing right now" from "work a
    process that no longer exists left behind", without a timeout heuristic and
    without guessing.
    """

    id: uuid.UUID
    started_at: datetime


async def register_runtime_generation(engine: AsyncEngine) -> RuntimeGeneration:
    generation_id = uuid.uuid4()
    async with engine.begin() as connection:
        started_at = await ActionRepository(connection).register_generation(generation_id)
    return RuntimeGeneration(id=generation_id, started_at=started_at)
