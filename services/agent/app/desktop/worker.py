"""The isolated desktop worker: an authenticated loopback service with two typed routes.

`POST /v1/desktop/surfaces` and `POST /v1/desktop/observe`. That is the whole surface.
There is no route that takes an action, a selector, coordinates, a script or a property
name, and unknown paths are 404.

Isolation is the process boundary. UI Automation runs on one dedicated thread (a COM
multithreaded apartment) inside this process, never on the runtime's event loop and
never in Electron main. A provider that hangs blocks that thread and nothing else; the
worker then reports `desktop_observation_timeout`, marks itself poisoned and exits, and
the runtime, which also enforces its own deadline, kills and fences this generation.
The thread cannot be trusted to come back, so the process is what gets replaced.

The credential is minted by the runtime for this start only and compared in constant
time. A request addressed to a different worker generation is refused, so an answer
from a replaced worker can never be mistaken for the current one.
"""

import asyncio
import logging
import os
import queue
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.browser.session import token_matches
from app.desktop.effects import CapturePlatform, DesktopEffects, EffectPlatform
from app.desktop.errors import HTTP_STATUS, DesktopReason, DesktopRefusal
from app.desktop.observer import DEFAULT_TIME_BUDGET_SECONDS, DesktopObserver, UiaBackend
from app.desktop.protocol import (
    WORKER_TOKEN_HEADER,
    CaptureRequest,
    FocusRequest,
    InputBaselineRequest,
    InvokeRequest,
    LaunchRequest,
    ObserveRequest,
    ScrollRequest,
    SelectRequest,
    SetValueRequest,
    SurfaceListRequest,
    SurfaceListResponse,
    WorkerErrorBody,
    WorkerIdentity,
)
from app.desktop.registry import AppRegistry
from app.desktop.surfaces import ExclusionPolicy, SurfaceTable, SystemProbe

logger = logging.getLogger("lumi.desktop.worker")

_T = TypeVar("_T")
OPERATIONS = [
    "surfaces", "observe", "input_baseline", "focus", "scroll", "launch",
    "set_value", "select", "invoke", "capture",
]


class WorkerSettings(BaseSettings):
    """Everything the worker is told. Deliberately tiny, and it never reads a `.env` file."""

    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", hide_input_in_errors=True, populate_by_name=True
    )

    token: SecretStr = Field(validation_alias="LUMI_DESKTOP_TOKEN", min_length=16)
    #: Comma-separated PIDs of the trusted processes whose whole process tree is Lumi's own.
    excluded_pids: str = Field(default="", validation_alias="LUMI_DESKTOP_EXCLUDED_PIDS")
    #: The hard deadline that separates "slow" from "hung". Generous on purpose: a large window is read
    #: under a soft time budget well inside it and comes back marked `time`; only a provider that
    #: does not answer at all should ever reach this and cost the worker its life.
    observation_timeout_seconds: float = Field(
        default=30.0, validation_alias="LUMI_DESKTOP_TIMEOUT_SECONDS", gt=0, le=120
    )
    #: The runtime created a kill-on-close job that every Lumi process (including this worker) is in.
    trust_job: bool = Field(default=False, validation_alias="LUMI_DESKTOP_TRUST_JOB")
    #: The user's registered applications (JSON), set by the runtime from trusted configuration.
    registered_apps: str = Field(default="", validation_alias="LUMI_DESKTOP_REGISTERED_APPS", max_length=8192)

    @field_validator("excluded_pids")
    @classmethod
    def _valid_pids(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip() and (not part.strip().isdigit() or int(part) < 1):
                raise ValueError("LUMI_DESKTOP_EXCLUDED_PIDS must be a comma-separated list of PIDs")
        return value

    @property
    def root_pids(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.excluded_pids.split(",") if part.strip())


class UiaThread:
    """One dedicated, abandonable thread. Every desktop call runs here.

    A daemon thread on purpose: if a provider hangs it, the process can still exit.
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[Callable[[], Any], Future[Any]]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="lumi-uia", daemon=True)
        self._thread.start()

    def submit(self, call: Callable[[], _T]) -> "Future[_T]":
        future: Future[_T] = Future()
        self._jobs.put((call, future))
        return future

    def _run(self) -> None:
        while True:
            call, future = self._jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(call())
            except BaseException as error:  # noqa: BLE001 - delivered to the awaiting request.
                future.set_exception(error)


@dataclass(slots=True)
class _State:
    generation: uuid.UUID
    started_at: datetime
    uia: UiaThread
    observer: DesktopObserver
    effects: DesktopEffects
    timeout: float
    poisoned: bool = False


class _LoopbackOnly:
    """Reject anything that is not addressed to 127.0.0.1 or that carries an Origin.

    A web page cannot reach this worker: it has no Origin allowance and cannot present
    the credential. This is the same reasoning as the runtime's own boundary.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            host = (headers.get("host") or "").partition(":")[0]
            if host != "127.0.0.1" or headers.get("origin") is not None:
                await Response(status_code=400)(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _error(code: DesktopReason | str, status_code: int, generation: uuid.UUID | None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=WorkerErrorBody(code=str(code), worker_generation=generation).model_dump(mode="json"),
    )


def _refusal(refusal: DesktopRefusal, generation: uuid.UUID) -> JSONResponse:
    return _error(refusal.code.value, HTTP_STATUS[refusal.code], generation)


def build_state(
    *,
    settings: WorkerSettings,
    probe_factory: Callable[[], SystemProbe],
    backend_factory: Callable[[], UiaBackend],
    platform_factory: Callable[[], EffectPlatform],
    capture_platform_factory: Callable[[], CapturePlatform] | None = None,
    registry: AppRegistry | None = None,
) -> _State:
    """Everything that touches COM or Win32 is created on the UIA thread, not the caller's."""
    uia = UiaThread()

    def initialise() -> tuple[DesktopObserver, DesktopEffects]:
        probe = probe_factory()
        exclusion = ExclusionPolicy.resolve(probe, settings.root_pids, trust_job=settings.trust_job)
        generation = uuid.uuid4()
        surfaces = SurfaceTable(probe=probe, exclusion=exclusion)
        backend = backend_factory()
        observer = DesktopObserver(
            surfaces=surfaces,
            backend=backend,
            worker_generation=generation,
            time_budget_seconds=min(DEFAULT_TIME_BUDGET_SECONDS, settings.observation_timeout_seconds * 0.4),
        )
        effects = DesktopEffects(
            surfaces=surfaces,
            observer=observer,
            backend=backend,
            platform=platform_factory(),
            registry=registry if registry is not None else AppRegistry.from_config(settings.registered_apps),
            capture_platform=capture_platform_factory() if capture_platform_factory is not None else None,
        )
        return observer, effects

    observer, effects = uia.submit(initialise).result(timeout=90)
    return _State(
        generation=observer.worker_generation,
        started_at=datetime.now(UTC),
        uia=uia,
        observer=observer,
        effects=effects,
        timeout=settings.observation_timeout_seconds,
    )


def create_worker_app(
    settings: WorkerSettings | None = None,
    *,
    probe_factory: Callable[[], SystemProbe] | None = None,
    backend_factory: Callable[[], UiaBackend] | None = None,
    platform_factory: Callable[[], EffectPlatform] | None = None,
    capture_platform_factory: Callable[[], CapturePlatform] | None = None,
    registry: AppRegistry | None = None,
    exit_process: Callable[[int], object] = os._exit,
) -> FastAPI:
    """Uvicorn calls this with no arguments. The keyword seams exist for tests only, and
    nothing in the environment or on the wire can reach them."""
    resolved = settings if settings is not None else WorkerSettings()
    if probe_factory is None or backend_factory is None or platform_factory is None:
        if os.name != "nt":
            raise DesktopRefusal(DesktopReason.UNSUPPORTED)
        from app.desktop.win32 import WindowsSystemProbe

        probe_factory = probe_factory or WindowsSystemProbe

        def real_backend() -> UiaBackend:
            # Imported here, on the UIA thread, so COM is initialised there and only there.
            from app.desktop.uia_backend import PywinautoBackend

            return PywinautoBackend()

        backend_factory = backend_factory or real_backend
        from app.desktop.effects_win32 import WindowsEffectPlatform

        platform_factory = platform_factory or WindowsEffectPlatform

        if capture_platform_factory is None:
            def real_capture_platform() -> CapturePlatform:
                from app.desktop.capture_win32 import WindowsCaptureBackend

                return WindowsCaptureBackend()

            capture_platform_factory = real_capture_platform

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = await asyncio.to_thread(
            build_state,
            settings=resolved,
            probe_factory=probe_factory,
            backend_factory=backend_factory,
            platform_factory=platform_factory,
            capture_platform_factory=capture_platform_factory,
            registry=registry,
        )
        app.state.desktop = state
        logger.info("desktop worker ready generation=%s", state.generation)
        yield

    def authenticate(token: Annotated[str, Header(alias=WORKER_TOKEN_HEADER)] = "") -> None:
        if not token_matches(token, resolved.token):
            # Never echo what was presented, and never say which part was wrong.
            raise _Unauthenticated

    router = APIRouter(dependencies=[Depends(authenticate)])

    def current(request: Request) -> _State:
        state: _State = request.app.state.desktop
        return state

    async def run(state: _State, call: Callable[[], _T]) -> _T:
        """Run on the UIA thread under a deadline. A blown deadline poisons this worker."""
        future = state.uia.submit(call)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=state.timeout)
        except TimeoutError:
            state.poisoned = True
            # The thread may never return. Replacing the process is the only real recovery.
            asyncio.get_running_loop().call_later(2.0, exit_process, 70)
            raise DesktopRefusal(DesktopReason.OBSERVATION_TIMEOUT) from None
        except DesktopRefusal:
            raise
        except Exception:  # noqa: BLE001
            # Anything else came from the desktop or the backend and may quote what it was reading.
            # It becomes one text-free code here, so no framework ever gets a traceback to log.
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None

    def guard(state: _State, expected: uuid.UUID) -> JSONResponse | None:
        if state.poisoned:
            return _error(DesktopReason.OBSERVATION_TIMEOUT, HTTP_STATUS[DesktopReason.OBSERVATION_TIMEOUT], state.generation)
        if expected != state.generation:
            return _error(
                DesktopReason.STALE_WORKER_GENERATION,
                HTTP_STATUS[DesktopReason.STALE_WORKER_GENERATION],
                state.generation,
            )
        return None

    @router.get("/health")
    async def health(request: Request) -> Response:
        state = current(request)
        return JSONResponse(
            WorkerIdentity(
                worker_generation=state.generation,
                started_at=state.started_at.isoformat(),
                platform="win32",
                operations=OPERATIONS,
            ).model_dump(mode="json")
        )

    @router.post("/v1/desktop/surfaces")
    async def list_surfaces(request: Request, body: SurfaceListRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        try:
            inventory = await run(state, state.observer.list_surfaces)
        except DesktopRefusal as refusal:
            return _refusal(refusal, state.generation)
        logger.info(
            "desktop inventory generation=%s surface_count=%d truncated=%s",
            state.generation, len(inventory.surfaces), inventory.truncated,
        )
        return JSONResponse(
            SurfaceListResponse(
                worker_generation=state.generation, surfaces=inventory.surfaces, truncated=inventory.truncated
            ).model_dump(mode="json")
        )

    @router.post("/v1/desktop/observe")
    async def observe(request: Request, body: ObserveRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        try:
            observation, stats = await run(
                state, lambda: state.observer.observe_measured(body.surface_ref, body.surface_epoch)
            )
        except DesktopRefusal as refusal:
            logger.info(
                "desktop observation refused generation=%s error_code=%s", state.generation, refusal.code.value
            )
            return _refusal(refusal, state.generation)
        logger.info(
            "desktop observation generation=%s observation_id=%s node_count=%d depth=%d truncated=%s duration_ms=%d",
            state.generation, observation.observation_id, stats.node_count, stats.depth,
            stats.truncated, stats.duration_ms,
        )
        return JSONResponse(observation.model_dump(mode="json"))

    async def effect(name: str, state: _State, call: Callable[[], BaseModel]) -> Response:
        try:
            answer = await run(state, call)
        except DesktopRefusal as refusal:
            logger.info("desktop %s refused generation=%s error_code=%s", name, state.generation, refusal.code.value)
            return _refusal(refusal, state.generation)
        logger.info("desktop %s generation=%s", name, state.generation)
        return JSONResponse(answer.model_dump(mode="json"))

    @router.post("/v1/desktop/input-baseline")
    async def input_baseline(request: Request, body: InputBaselineRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("input_baseline", state, state.effects.input_baseline)

    @router.post("/v1/desktop/focus")
    async def focus(request: Request, body: FocusRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("focus", state, lambda: state.effects.focus(body))

    @router.post("/v1/desktop/scroll")
    async def scroll(request: Request, body: ScrollRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("scroll", state, lambda: state.effects.scroll(body))

    @router.post("/v1/desktop/launch")
    async def launch(request: Request, body: LaunchRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("launch", state, lambda: state.effects.launch(body))

    @router.post("/v1/desktop/set-value")
    async def set_value(request: Request, body: SetValueRequest) -> Response:
        # `effect()` and this route never log or echo `body.value`: only the outcome enum and the
        # generation are ever written down.
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("set_value", state, lambda: state.effects.set_value(body))

    @router.post("/v1/desktop/select")
    async def select(request: Request, body: SelectRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("select", state, lambda: state.effects.select(body))

    @router.post("/v1/desktop/invoke")
    async def invoke(request: Request, body: InvokeRequest) -> Response:
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("invoke", state, lambda: state.effects.invoke(body))

    @router.post("/v1/desktop/capture")
    async def capture(request: Request, body: CaptureRequest) -> Response:
        # `effect()` and this route never log the image: only the outcome and the generation are ever
        # written down, exactly like `set_value` never logs the value it wrote.
        state = current(request)
        refused = guard(state, body.expected_worker_generation)
        if refused is not None:
            return refused
        return await effect("capture", state, lambda: state.effects.capture(body))

    app = FastAPI(title="Lumi Desktop Worker", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)

    @app.exception_handler(_Unauthenticated)
    async def unauthenticated(_: Request, __: Exception) -> Response:
        logger.warning("rejected an unauthenticated desktop-worker request")
        return _error("unauthenticated", 401, None)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, _: Exception) -> Response:
        try:
            generation: uuid.UUID | None = request.app.state.desktop.generation
        except AttributeError:
            generation = None
        return _error(DesktopReason.BACKEND_FAILED, 502, generation)

    app.add_middleware(_LoopbackOnly)
    return app


class _Unauthenticated(Exception):
    pass

