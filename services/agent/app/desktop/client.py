"""The runtime's side of the channel to the desktop worker.

Two jobs, both about not believing things:

* **Classify a failure honestly and without text.** Observation has no external
  effect, so there is no "outcome unknown": every failure maps to a stable
  `DesktopRefusal` code. A connection that could not be made and one that dropped
  mid-call are both `desktop_worker_unavailable`; a call that ran past its deadline is
  `desktop_observation_timeout`, and the supervisor kills that worker generation.
* **Refuse an answer that is not addressed to us.** A response from another worker
  generation, or about another surface, is discarded: it may be a late reply from a
  worker that has since been replaced.
"""

from types import TracebackType
from typing import TypeVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import (
    WORKER_TOKEN_HEADER,
    DesktopObservation,
    FocusRequest,
    FocusResponse,
    InputBaselineRequest,
    InputBaselineResponse,
    LaunchRequest,
    LaunchResponse,
    ObserveRequest,
    ScrollRequest,
    ScrollResponse,
    SurfaceListRequest,
    SurfaceListResponse,
    WorkerErrorBody,
    WorkerIdentity,
)

_Response = TypeVar("_Response", bound=BaseModel)


class StaleDesktopResult(Exception):
    """An effect answer not addressed to the dispatch that was sent. The effect may have happened."""


class DesktopWorkerClient:
    """A narrow, authenticated, loopback-only client. Not a general HTTP client."""

    def __init__(self, *, base_url: str, token: SecretStr, timeout_seconds: float) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            # Loopback to one process, never through a proxy named by the environment or the
            # registry: that would send the worker credential and desktop text to the proxy.
            trust_env=False,
            # Set once on the client so no call site can forget it and no URL can carry it.
            headers={WORKER_TOKEN_HEADER: token.get_secret_value()},
        )

    async def __aenter__(self) -> "DesktopWorkerClient":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def identify(self) -> WorkerIdentity:
        response = await self._send("GET", "/health")
        return self._parse(response, WorkerIdentity)

    async def list_surfaces(self, request: SurfaceListRequest) -> SurfaceListResponse:
        response = await self._send("POST", "/v1/desktop/surfaces", request)
        answer = self._parse(response, SurfaceListResponse)
        if answer.worker_generation != request.expected_worker_generation:
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        return answer

    async def observe(self, request: ObserveRequest) -> DesktopObservation:
        response = await self._send("POST", "/v1/desktop/observe", request)
        answer = self._parse(response, DesktopObservation)
        if (
            answer.worker_generation != request.expected_worker_generation
            or answer.surface_ref != request.surface_ref
            # The epoch may have moved forward (the structure changed); it never moves back.
            or answer.surface_epoch < request.surface_epoch
        ):
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        return answer

    async def input_baseline(self, request: InputBaselineRequest) -> InputBaselineResponse:
        response = await self._send("POST", "/v1/desktop/input-baseline", request)
        answer = self._parse(response, InputBaselineResponse)
        if answer.worker_generation != request.expected_worker_generation:
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        return answer

    async def focus(self, request: FocusRequest) -> FocusResponse:
        response = await self._send("POST", "/v1/desktop/focus", request)
        answer = self._parse(response, FocusResponse)
        if (
            answer.worker_generation != request.expected_worker_generation
            or answer.dispatch_id != request.dispatch_id
            or (answer.surface_ref, answer.surface_epoch) != (request.surface_ref, request.surface_epoch)
        ):
            raise StaleDesktopResult
        return answer

    async def scroll(self, request: ScrollRequest) -> ScrollResponse:
        response = await self._send("POST", "/v1/desktop/scroll", request)
        answer = self._parse(response, ScrollResponse)
        if answer.worker_generation != request.expected_worker_generation or answer.dispatch_id != request.dispatch_id:
            raise StaleDesktopResult
        return answer

    async def launch(self, request: LaunchRequest) -> LaunchResponse:
        response = await self._send("POST", "/v1/desktop/launch", request)
        answer = self._parse(response, LaunchResponse)
        if (
            answer.worker_generation != request.expected_worker_generation
            or answer.dispatch_id != request.dispatch_id
            or answer.app_id != request.app_id
        ):
            raise StaleDesktopResult
        return answer

    async def _send(self, method: str, path: str, body: BaseModel | None = None) -> httpx.Response:
        try:
            if body is None:
                response = await self._client.request(method, path)
            else:
                response = await self._client.request(method, path, json=body.model_dump(mode="json"))
        except httpx.TimeoutException:
            raise DesktopRefusal(DesktopReason.OBSERVATION_TIMEOUT) from None
        except httpx.HTTPError:
            raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE) from None
        if response.status_code >= 400:
            raise _rejection(response)
        return response

    @staticmethod
    def _parse(response: httpx.Response, model: type[_Response]) -> _Response:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE) from None


def _rejection(response: httpx.Response) -> DesktopRefusal:
    """The worker's code if it is one we know; otherwise a generic, text-free failure."""
    if response.status_code == 401:
        return DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE)
    try:
        body = WorkerErrorBody.model_validate(response.json())
        return DesktopRefusal(DesktopReason(body.code))
    except (ValueError, ValidationError):
        return DesktopRefusal(DesktopReason.BACKEND_FAILED)
