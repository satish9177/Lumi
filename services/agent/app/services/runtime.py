import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from app.repositories.actions import ActionRepository


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
