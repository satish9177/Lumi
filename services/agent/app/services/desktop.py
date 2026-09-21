"""The runtime's desktop observation service (Milestone 9, slice 1).

Two operations: list the user-visible surfaces, and observe one. Both go to the
isolated desktop worker; neither can change anything on the desktop.

This is where the worker's containment becomes the runtime's behaviour. Every call runs
under a deadline of its own, and any timeout, dropped connection or answer addressed
to another generation kills and fences the worker, so the next call starts a fresh
generation. Observation has no external effect, so a failed read is simply retried as a
new read; there is no "outcome unknown" to reconcile.

The durable record is the safe projection only. What a caller receives is the same
closed schema, and it is *not* handed to any model, memory, research context or task
summary in this slice: nothing outside this module and its routes refers to it.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.client import DesktopWorkerClient
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.managed import DesktopEndpoint, ManagedDesktopWorker
from app.desktop.protocol import (
    DesktopObservation,
    ObserveRequest,
    SurfaceListRequest,
    SurfaceListResponse,
)
from app.domain.digest import canonical_json
from app.repositories.desktop import DesktopRepository

logger = logging.getLogger("lumi.desktop")

#: Recent observations retained locally. Desktop text is private; it is not kept indefinitely.
RETAINED_OBSERVATIONS = 25
#: ...and for no longer than this, however few there are.
RETAINED_SECONDS = 24 * 60 * 60
#: The worker enforces its own deadline first; the runtime waits a little longer, then kills it.
DEADLINE_MARGIN_SECONDS = 3.0

#: A failure after which nothing this worker generation says can be trusted.
_FENCING = frozenset(
    {
        DesktopReason.OBSERVATION_TIMEOUT,
        DesktopReason.WORKER_UNAVAILABLE,
        DesktopReason.STALE_WORKER_GENERATION,
        DesktopReason.BACKEND_FAILED,
    }
)


def desktop_exclusion_roots(runtime_pid: int, electron_pid: int | None) -> tuple[int, ...]:
    """The trusted processes whose whole process tree is Lumi's own and never a target.

    The runtime is always one, so its browser worker, every Chromium it owns (sign-in,
    takeover and form-preparation windows) and this desktop worker are covered. When
    Electron supervises the runtime, Electron main is the other: that covers the
    renderer, DevTools and the GPU and utility processes.
    """
    roots = {runtime_pid}
    if electron_pid is not None:
        roots.add(electron_pid)
    return tuple(sorted(roots))


class _CallerIsStale(DesktopRefusal):
    """The *caller* holds a reference to a worker generation that no longer exists.

    Not a fault of the current worker, so it must not fence it: killing a healthy, freshly started
    worker because somebody presented an old pair would let a stale caller keep the worker in a loop.
    """

    def __init__(self) -> None:
        super().__init__(DesktopReason.STALE_WORKER_GENERATION)


@dataclass(frozen=True, slots=True)
class _Bound:
    endpoint: DesktopEndpoint
    generation: uuid.UUID


class DesktopService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        runtime_generation: uuid.UUID,
        worker: ManagedDesktopWorker | None,
        timeout_seconds: float,
        unsupported: bool = False,
    ) -> None:
        self._engine = engine
        self._runtime_generation = runtime_generation
        self._worker = worker
        self._deadline = timeout_seconds + DEADLINE_MARGIN_SECONDS
        self._unsupported = unsupported
        self._lock = asyncio.Lock()
        self._bound: _Bound | None = None

    # -- operations ---------------------------------------------------------------

    async def list_surfaces(self) -> SurfaceListResponse:
        async def call(client: DesktopWorkerClient, generation: uuid.UUID) -> SurfaceListResponse:
            return await client.list_surfaces(SurfaceListRequest(expected_worker_generation=generation))

        return await self._run(call)

    async def observe(
        self, worker_generation: uuid.UUID, surface_ref: str, surface_epoch: int
    ) -> DesktopObservation:
        """Observe one surface, but only in the worker generation that issued the pair.

        A `(surface_ref, surface_epoch)` is unique within one worker generation, not across them (a
        new worker starts every slot again), so the generation the caller listed under is part of
        the request and is checked before anything is read.
        """

        async def call(client: DesktopWorkerClient, generation: uuid.UUID) -> DesktopObservation:
            if generation != worker_generation:
                raise _CallerIsStale
            return await client.observe(
                ObserveRequest(
                    expected_worker_generation=generation,
                    surface_ref=surface_ref,
                    surface_epoch=surface_epoch,
                )
            )

        observation = await self._run(call)
        await self._persist(observation)
        return observation

    # -- plumbing -----------------------------------------------------------------

    async def _run[T](
        self, call: Callable[[DesktopWorkerClient, uuid.UUID], Awaitable[T]]
    ) -> T:
        worker = self._require_worker()
        # One desktop operation at a time: the worker has one UIA thread, and a second caller
        # queuing behind a hung provider would only inherit its timeout.
        async with self._lock:
            endpoint = await worker.endpoint()
            try:
                async with asyncio.timeout(self._deadline):
                    async with DesktopWorkerClient(
                        base_url=endpoint.base_url,
                        token=endpoint.token,
                        timeout_seconds=endpoint.timeout_seconds + DEADLINE_MARGIN_SECONDS,
                    ) as client:
                        generation = await self._bind(client, endpoint)
                        return await call(client, generation)
            except TimeoutError:
                await self._fence(worker)
                raise DesktopRefusal(DesktopReason.OBSERVATION_TIMEOUT) from None
            except DesktopRefusal as refusal:
                if refusal.code in _FENCING and not isinstance(refusal, _CallerIsStale):
                    await self._fence(worker)
                raise

    def _require_worker(self) -> ManagedDesktopWorker:
        if self._unsupported:
            raise DesktopRefusal(DesktopReason.UNSUPPORTED)
        if self._worker is None:
            raise DesktopRefusal(DesktopReason.DISABLED)
        return self._worker

    async def _bind(self, client: DesktopWorkerClient, endpoint: DesktopEndpoint) -> uuid.UUID:
        """Learn which worker generation this endpoint is, and record it once."""
        if self._bound is not None and self._bound.endpoint is endpoint:
            return self._bound.generation
        identity = await client.identify()
        async with self._engine.begin() as connection:
            await DesktopRepository(connection).register_worker_generation(
                worker_generation=identity.worker_generation,
                runtime_generation=self._runtime_generation,
                worker_started_at=datetime.fromisoformat(identity.started_at),
            )
        assert self._worker is not None
        self._worker.mark_healthy()
        self._bound = _Bound(endpoint=endpoint, generation=identity.worker_generation)
        return identity.worker_generation

    async def _fence(self, worker: ManagedDesktopWorker) -> None:
        self._bound = None
        await worker.fence()
        logger.warning("desktop worker fenced")

    async def _persist(self, observation: DesktopObservation) -> None:
        snapshot = observation.model_dump(mode="json")
        digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
        try:
            async with self._engine.begin() as connection:
                repository = DesktopRepository(connection)
                await repository.insert_observation(
                    observation_id=observation.observation_id,
                    worker_generation=observation.worker_generation,
                    surface_ref=observation.surface_ref,
                    surface_epoch=observation.surface_epoch,
                    schema_version=observation.schema_version,
                    classification=observation.classification,
                    snapshot=snapshot,
                    snapshot_digest=digest,
                    truncated=observation.truncated,
                )
                await repository.prune(keep=RETAINED_OBSERVATIONS, max_age_seconds=RETAINED_SECONDS)
        except SQLAlchemyError:
            # A database error quotes the statement parameters, and one of them is the observed text.
            logger.error("a desktop observation could not be stored")
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None

    async def sweep_expired(self) -> None:
        """Startup housekeeping: drop observations past their retention, even with the capability off."""
        async with self._engine.begin() as connection:
            await DesktopRepository(connection).prune(keep=RETAINED_OBSERVATIONS, max_age_seconds=RETAINED_SECONDS)
