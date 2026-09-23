"""SQL for the cross-executor effect lock (Milestone 10 S2). Callers own the transaction.

Serialisation across tasks: the ledger's usual lock is the owning task's row, which says nothing about
another task. `lock` takes `pg_advisory_xact_lock` on each key (hashed, in sorted order), so two claims
that share a key -- from any task and any executor -- run one after the other, and the second one sees
the first's attempt when it runs `conflicts`.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import and_, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import action_effect_keys, actions
from app.domain.action_status import ActionStatus
from app.domain.effects import GLOBAL_TIER, EffectKey

#: Statuses in which an effect may have happened and nobody knows yet, or is happening now.
UNRESOLVED_STATUSES = (ActionStatus.EXECUTING.value, ActionStatus.OUTCOME_UNKNOWN.value, ActionStatus.RECONCILING.value)
#: The global tier blocks on genuine uncertainty; an in-flight run is handled by its own key.
GLOBAL_BLOCKING_STATUSES = (ActionStatus.OUTCOME_UNKNOWN.value, ActionStatus.RECONCILING.value)
#: A fixed namespace so these advisory locks cannot collide with the runtime-ownership lock.
_NAMESPACE = "lumi-effect-lock:"


class EffectLockRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_keys(self, *, action_id: uuid.UUID, keys: Iterable[EffectKey]) -> None:
        rows = [{"action_id": action_id, "effect_key": key.key, "effect_kind": key.kind.value} for key in keys]
        if rows:
            await self._connection.execute(insert(action_effect_keys), rows)

    async def keys_for(self, action_id: uuid.UUID) -> list[str]:
        result = await self._connection.execute(
            select(action_effect_keys.c.effect_key).where(action_effect_keys.c.action_id == action_id)
        )
        return sorted(row.effect_key for row in result)

    async def lock(self, keys: Iterable[str]) -> None:
        for key in sorted(set(keys)):
            await self._connection.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(_NAMESPACE + key, 0))))

    async def conflict(self, *, action_id: uuid.UUID, keys: Iterable[str]) -> tuple[uuid.UUID, str] | None:
        """Another action sharing a key that is in flight or unresolved, or any unresolved global-tier effect."""
        wanted = sorted(set(keys))
        if wanted:
            row = (
                await self._connection.execute(
                    select(actions.c.id)
                    .select_from(action_effect_keys.join(actions, actions.c.id == action_effect_keys.c.action_id))
                    .where(
                        action_effect_keys.c.effect_key.in_(wanted),
                        actions.c.id != action_id,
                        actions.c.status.in_(UNRESOLVED_STATUSES),
                    )
                    .limit(1)
                )
            ).first()
            if row is not None:
                return row.id, "same_effect_unresolved"
        row = (
            await self._connection.execute(
                select(actions.c.id)
                .select_from(action_effect_keys.join(actions, actions.c.id == action_effect_keys.c.action_id))
                .where(
                    and_(
                        action_effect_keys.c.effect_kind.in_(sorted(kind.value for kind in GLOBAL_TIER)),
                        actions.c.id != action_id,
                        actions.c.status.in_(GLOBAL_BLOCKING_STATUSES),
                    )
                )
                .limit(1)
            )
        ).first()
        if row is not None:
            return row.id, "consequential_effect_unresolved"
        return None
