"""The runtime's side of the channel to the browser worker.

Two responsibilities, and they are both about not believing things:

* **Classify a failed call honestly.** A refused connection means the dispatch
  never left, so nothing happened. A dropped connection or a timeout means the
  request was delivered and the answer was not -- which, for a consequential
  operation, is precisely `OUTCOME_UNKNOWN`. Collapsing those two into "the
  worker call failed" is how an agent retries a booking it already made.

* **Refuse an answer that is not addressed to us.** A response carrying another
  runtime generation, another worker generation, or another dispatch id is
  discarded. It might be a late reply from a worker we have since replaced, and
  writing it into the ledger would mean recording an outcome produced by a
  process nobody is talking to any more.
"""

import logging
import uuid
from types import TracebackType

import httpx
from pydantic import SecretStr, ValidationError

from app.browser.errors import (
    BrowserWorkerError,
    BrowserWorkerLostResponseError,
    BrowserWorkerRejectedError,
    BrowserWorkerUnavailableError,
    StaleWorkerResultError,
)
from app.browser.protocol import (
    WORKER_TOKEN_HEADER,
    DispatchRequest,
    DispatchResponse,
    ProfileSessionRequest,
    ProfileSessionResponse,
    SessionRequest,
    SessionResponse,
    TakeoverConfirmRequest,
    TakeoverConfirmResponse,
    TakeoverStartRequest,
    TakeoverStartResponse,
    WorkerErrorBody,
    WorkerIdentity,
)

logger = logging.getLogger("lumi.browser.client")


class BrowserWorkerClient:
    """A narrow, authenticated, loopback-only client. Not a general HTTP client."""

    def __init__(
        self,
        *,
        base_url: str,
        token: SecretStr,
        runtime_generation: uuid.UUID,
        timeout_seconds: float,
    ) -> None:
        self._runtime_generation = runtime_generation
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            # The credential is set once, on the client, so no call site can
            # forget it and no URL can carry it.
            headers={WORKER_TOKEN_HEADER: token.get_secret_value()},
        )

    async def __aenter__(self) -> "BrowserWorkerClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def identify(self) -> WorkerIdentity:
        """Handshake. Establishes which worker generation we are talking to."""
        try:
            response = await self._client.get("/health")
        except httpx.ConnectError as error:
            raise BrowserWorkerUnavailableError(f"connection failed ({type(error).__name__})")
        except httpx.HTTPError as error:
            raise BrowserWorkerUnavailableError(f"{type(error).__name__}")
        if response.status_code == 401:
            raise BrowserWorkerRejectedError("the worker credential was not accepted", status_code=401)
        if response.status_code != 200:
            raise BrowserWorkerUnavailableError(f"health returned {response.status_code}")
        try:
            return WorkerIdentity.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise BrowserWorkerUnavailableError(f"unreadable health response: {type(error).__name__}")

    async def open_session(self, request: SessionRequest) -> SessionResponse:
        """Ask the worker to create the task-owned research context."""
        return await self._session_call("/v1/sessions/open", request)

    async def close_session(self, request: SessionRequest) -> SessionResponse:
        """Dispose of the context and every semantic ref it issued."""
        return await self._session_call("/v1/sessions/close", request)

    async def _session_call(self, path: str, request: SessionRequest) -> SessionResponse:
        try:
            response = await self._client.post(path, json=request.model_dump(mode="json"))
        except httpx.ConnectError:
            raise BrowserWorkerUnavailableError("the worker refused the connection")
        except httpx.HTTPError as error:
            raise BrowserWorkerLostResponseError(type(error).__name__)
        if response.status_code >= 400:
            raise _rejection(response)
        try:
            answer = SessionResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise BrowserWorkerLostResponseError(
                f"the worker returned an unreadable result ({type(error).__name__})"
            )
        if answer.session_id != request.session_id:
            raise StaleWorkerResultError("it answers a different session")
        if answer.worker_generation != request.expected_worker_generation:
            raise StaleWorkerResultError("it came from a different worker generation")
        return answer

    async def open_profile(self, request: ProfileSessionRequest) -> ProfileSessionResponse:
        """Ask the worker to open one persistent, Lumi-managed profile."""
        return await self._profile_call("/v1/profiles/open", request)

    async def close_profile(self, request: ProfileSessionRequest) -> ProfileSessionResponse:
        """Close the persistent context and release its exclusive OS handle."""
        return await self._profile_call("/v1/profiles/close", request)

    async def _profile_call(
        self, path: str, request: ProfileSessionRequest
    ) -> ProfileSessionResponse:
        try:
            response = await self._client.post(path, json=request.model_dump(mode="json"))
        except httpx.ConnectError:
            raise BrowserWorkerUnavailableError("the worker refused the connection")
        except httpx.HTTPError as error:
            raise BrowserWorkerLostResponseError(type(error).__name__)
        if response.status_code >= 400:
            raise _rejection(response)
        try:
            answer = ProfileSessionResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise BrowserWorkerLostResponseError(
                f"the worker returned an unreadable result ({type(error).__name__})"
            )
        if answer.profile_id != request.profile_id:
            raise StaleWorkerResultError("it answers a different profile")
        if answer.worker_generation != request.expected_worker_generation:
            raise StaleWorkerResultError("it came from a different worker generation")
        return answer

    async def start_takeover(self, request: TakeoverStartRequest) -> TakeoverStartResponse:
        """Ask the worker to navigate the profile's headed tab and hand it over."""
        try:
            response = await self._client.post(
                "/v1/profiles/takeover/start", json=request.model_dump(mode="json")
            )
        except httpx.ConnectError:
            raise BrowserWorkerUnavailableError("the worker refused the connection")
        except httpx.HTTPError as error:
            raise BrowserWorkerLostResponseError(type(error).__name__)
        if response.status_code >= 400:
            raise _rejection(response)
        try:
            answer = TakeoverStartResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise BrowserWorkerLostResponseError(
                f"the worker returned an unreadable result ({type(error).__name__})"
            )
        if answer.profile_id != request.profile_id:
            raise StaleWorkerResultError("it answers a different profile")
        if answer.worker_generation != request.expected_worker_generation:
            raise StaleWorkerResultError("it came from a different worker generation")
        return answer

    async def confirm_takeover(self, request: TakeoverConfirmRequest) -> TakeoverConfirmResponse:
        """Ask the worker to run the deterministic post-takeover check."""
        try:
            response = await self._client.post(
                "/v1/profiles/takeover/confirm", json=request.model_dump(mode="json")
            )
        except httpx.ConnectError:
            raise BrowserWorkerUnavailableError("the worker refused the connection")
        except httpx.HTTPError as error:
            raise BrowserWorkerLostResponseError(type(error).__name__)
        if response.status_code >= 400:
            raise _rejection(response)
        try:
            answer = TakeoverConfirmResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise BrowserWorkerLostResponseError(
                f"the worker returned an unreadable result ({type(error).__name__})"
            )
        if answer.profile_id != request.profile_id:
            raise StaleWorkerResultError("it answers a different profile")
        if answer.worker_generation != request.expected_worker_generation:
            raise StaleWorkerResultError("it came from a different worker generation")
        return answer

    async def dispatch(self, request: DispatchRequest) -> DispatchResponse:
        try:
            response = await self._client.post(
                "/v1/dispatch", json=request.model_dump(mode="json")
            )
        except httpx.ConnectError:
            # Never delivered. The worker did not act.
            raise BrowserWorkerUnavailableError("the worker refused the connection")
        except httpx.HTTPError as error:
            # Delivered, then the answer was lost. The worker may have acted.
            raise BrowserWorkerLostResponseError(type(error).__name__)

        if response.status_code >= 400:
            raise _rejection(response)

        try:
            answer = DispatchResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise BrowserWorkerLostResponseError(
                f"the worker returned an unreadable result ({type(error).__name__})"
            )

        self._check_addressed_to_us(request, answer)
        return answer

    def _check_addressed_to_us(
        self, request: DispatchRequest, answer: DispatchResponse
    ) -> None:
        if answer.runtime_generation != self._runtime_generation:
            raise StaleWorkerResultError("it names a different runtime generation")
        if answer.worker_generation != request.expected_worker_generation:
            raise StaleWorkerResultError("it came from a different worker generation")
        if answer.dispatch_id != request.dispatch_id:
            raise StaleWorkerResultError("it answers a different dispatch")
        if answer.operation != request.operation:
            raise StaleWorkerResultError("it answers a different operation")


def _rejection(response: httpx.Response) -> BrowserWorkerError:
    """Turn the worker's error body into the right kind of error.

    A refusal the worker made *before* running anything is a known failure. A
    `duplicate_dispatch` is the one case that is not: it means this dispatch is
    already in flight on the worker, so whatever it is doing may well succeed,
    and treating it as a failure would be a claim we cannot support.
    """
    try:
        body = WorkerErrorBody.model_validate(response.json())
        code, message = body.code, body.message
    except (ValueError, ValidationError):
        code, message = "worker_error", f"HTTP {response.status_code}"

    if code == "duplicate_dispatch":
        return BrowserWorkerLostResponseError(
            "this dispatch is already running on the worker; its outcome is not ours to guess"
        )
    return BrowserWorkerRejectedError(f"{code}: {message}", status_code=response.status_code)
