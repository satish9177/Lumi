"""One read-only browser observation on behalf of a task.

Search, slot preparation and the clinic-information lookup all ask the isolated
worker to *read* a reviewed page before anything durable exists for it. Such a
dispatch carries no action and no attempt, so it is never written to the
per-action dispatch ledger; it is logged instead. A read-only operation that
reports a submission is refused outright.
"""

import logging
import time
import uuid
from typing import Any

from app.browser.config import DEFAULT_SITE
from app.browser.protocol import DispatchRequest, OperationStatus
from app.domain.errors import BrowserObservationError
from app.services.browser_execution import WorkerSource, open_worker_client

logger = logging.getLogger("lumi.browser.observation")


async def observe_read_only(
    worker: WorkerSource | None,
    runtime_generation: uuid.UUID,
    *,
    operation: str,
    payload: dict[str, Any],
    task_id: uuid.UUID,
) -> tuple[OperationStatus, dict[str, Any]]:
    client = await open_worker_client(worker, runtime_generation)
    started = time.monotonic()
    try:
        identity = await client.identify()
        request = DispatchRequest(
            dispatch_id=uuid.uuid4(),
            runtime_generation=runtime_generation,
            expected_worker_generation=identity.worker_generation,
            action_id=None,
            attempt_id=None,
            operation=operation,
            site=DEFAULT_SITE,
            input=payload,
        )
        response = await client.dispatch(request)
    finally:
        await client.aclose()
    logger.info(
        "read-only browser observation finished",
        extra={
            "task_id": str(task_id),
            "operation": operation,
            "dispatch_id": str(request.dispatch_id),
            "worker_generation": str(identity.worker_generation),
            "status": response.status.value,
            "submitted": response.submitted,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    if response.submitted:  # pragma: no cover - read-only operations never submit.
        raise BrowserObservationError("unexpected_submission")
    return response.status, response.observation
