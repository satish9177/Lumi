from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorDetail, ErrorResponse
from app.browser.errors import (
    BrowserExecutionNotSupportedError,
    BrowserWorkerError,
    BrowserWorkerNotConfiguredError,
)
from app.domain.booking import BookingProposalError
from app.domain.page_observation import AnswerNotGroundedError
from app.domain.research import AnswerNotGroundedError as ResearchAnswerNotGroundedError
from app.desktop.errors import DesktopRefusal
from app.services.desktop_actions import DesktopActionError
from app.domain.browser_profile import ProfileRefusal
from app.domain.login_takeover import TakeoverRefusal
from app.domain.research import ResearchRefusal
from app.domain.authenticated import AuthenticatedRefusal
from app.domain.documents import STATE_CODES as DOCUMENT_STATE_CODES
from app.domain.documents import DocumentRefusal
from app.domain.effects import EffectLockedError
from app.domain.projects import STATE_CODES as PROJECT_STATE_CODES
from app.domain.projects import ProjectRefusal
from app.domain.transfers import STATE_CODES as TRANSFER_STATE_CODES
from app.domain.transfers import TransferRefusal
from app.domain.desktop_disclosure import STATE_CODES as DESKTOP_DISCLOSURE_STATE_CODES
from app.domain.desktop_disclosure import DesktopDisclosureRefusal
from app.domain.desktop_planning import STATE_CODES as DESKTOP_PLAN_STATE_CODES
from app.domain.desktop_planning import DesktopPlanRefusal
from app.domain.desktop_vision import STATE_CODES as DESKTOP_VISION_STATE_CODES
from app.domain.desktop_vision import DesktopVisionRefusal
from app.domain.form_prepare import FORM_PREPARE_STATE_CODES, FormPrepareRefusal
from app.domain.protected_values import ProtectedValueRefusal
from app.services.research_search import SearchFailedError
from app.domain.errors import (
    AuthenticatedAnswerAlreadyRecordedError,
    AuthenticatedBudgetExhaustedError,
    AuthenticatedGrantNotFoundError,
    AuthenticatedGrantNotUsableError,
    AuthenticatedProfileUnavailableError,
    AuthenticatedReadNotConfiguredError,
    AuthenticatedStepInFlightError,
    AuthenticatedStepRefusedError,
    BookingCriteriaError,
    BookingCriteriaMismatchError,
    TaskAlreadyBookedError,
    TaskHasUnresolvedActionError,
    ActionAlreadyOpenError,
    ActionConcurrencyError,
    BookingSlotUnavailableError,
    BrowserObservationError,
    ActionNotFoundError,
    ActionProposalConflictError,
    ApprovalNotUsableError,
    AttemptNotFoundError,
    InvalidActionTransitionError,
    StaleActionRevisionError,
    StaleTaskRevisionError,
    TaskConcurrencyError,
    TaskNotAcceptingActionsError,
    TaskNotCancellableError,
    TaskKindMismatchError,
    TaskNotFoundError,
    DestinationNotAllowedError,
    InspectionProposalError,
    ObservationNotAvailableError,
    PublicInspectionNotConfiguredError,
    ResearchAnswerAlreadyRecordedError,
    ResearchBudgetExhaustedError,
    ResearchGrantNotFoundError,
    ResearchGrantNotUsableError,
    ResearchNotConfiguredError,
    ResearchSessionUnavailableError,
    ResearchStepInFlightError,
    ResearchStepRefusedError,
    StaleObservationError,
)


def _error(status_code: int, detail: ErrorDetail) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=detail).model_dump(mode="json", exclude_none=True),
    )


Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _simple(status_code: int, code: str) -> Handler:
    """One handler for errors whose message is the whole story."""

    async def handler(_: Request, exc: Exception) -> JSONResponse:
        return _error(status_code, ErrorDetail(code=code, message=str(exc)))

    return handler


async def _stale_task_revision(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StaleTaskRevisionError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(code="stale_revision", message=str(exc), current_revision=exc.current_revision),
    )


async def _stale_action_revision(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StaleActionRevisionError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(
            code="stale_action_revision",
            message=str(exc),
            current_revision=exc.current_revision,
        ),
    )


async def _approval_not_usable(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApprovalNotUsableError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(code="approval_not_usable", message=str(exc), reason=exc.reason),
    )


async def _destination_not_allowed(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DestinationNotAllowedError)
    return _error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorDetail(
            code="destination_not_allowed",
            message="That page is not an allowed inspection destination.",
            reason=exc.code,
        ),
    )


async def _answer_not_grounded(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AnswerNotGroundedError)
    return _error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorDetail(
            code="answer_not_grounded",
            message="The answer is not supported by the stored observation.",
            reason=exc.code,
        ),
    )


def _reasoned(status_code: int, code: str, message: str, attribute: str) -> Handler:
    """A stable code plus one machine-readable reason. Never internal text."""

    async def handler(_: Request, exc: Exception) -> JSONResponse:
        reason = getattr(exc, attribute, None)
        return _error(
            status_code,
            ErrorDetail(
                code=code,
                message=message,
                reason=str(reason) if isinstance(reason, str) else None,
            ),
        )

    return handler


async def _desktop_refused(_: Request, exc: Exception) -> JSONResponse:
    """Milestone 9 S1. A stable code and one closed reason, never a window title, a name
    or any text observed on the desktop."""
    assert isinstance(exc, DesktopRefusal)
    return _error(
        exc.http_status,
        ErrorDetail(
            code="desktop_refused",
            message="That desktop operation was refused.",
            reason=exc.code.value,
        ),
    )


async def _desktop_action_refused(_: Request, exc: Exception) -> JSONResponse:
    """Milestone 9 S3. A closed code, never a title, a name, a path or observed text."""
    assert isinstance(exc, DesktopActionError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(code="desktop_action_refused", message="That desktop action was refused.", reason=exc.code),
    )


def _fixed(status_code: int, code: str, message: str) -> Handler:
    """For errors whose text may carry internal detail: a fixed safe message."""

    async def handler(_: Request, __: Exception) -> JSONResponse:
        return _error(status_code, ErrorDetail(code=code, message=message))

    return handler


def register_error_handlers(app: FastAPI) -> None:
    async def invalid_request(_: Request, __: Exception) -> JSONResponse:
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(code="invalid_request", message="Request validation failed."),
        )

    # FastAPI's default includes rejected input values. Runtime callers need a
    # stable code, never an echo of proposal text or malformed credentials.
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(
        TaskNotFoundError, _simple(status.HTTP_404_NOT_FOUND, "task_not_found")
    )
    app.add_exception_handler(
        ActionNotFoundError, _simple(status.HTTP_404_NOT_FOUND, "action_not_found")
    )
    app.add_exception_handler(
        TaskNotCancellableError, _simple(status.HTTP_409_CONFLICT, "task_not_cancellable")
    )
    app.add_exception_handler(
        TaskNotAcceptingActionsError,
        _simple(status.HTTP_409_CONFLICT, "task_not_accepting_actions"),
    )
    app.add_exception_handler(
        ActionProposalConflictError,
        _simple(status.HTTP_409_CONFLICT, "action_proposal_conflict"),
    )
    app.add_exception_handler(
        InvalidActionTransitionError,
        _simple(status.HTTP_409_CONFLICT, "invalid_action_transition"),
    )
    app.add_exception_handler(
        AttemptNotFoundError, _simple(status.HTTP_409_CONFLICT, "no_unfinished_attempt")
    )
    app.add_exception_handler(
        TaskConcurrencyError, _simple(status.HTTP_409_CONFLICT, "concurrent_modification")
    )
    app.add_exception_handler(
        ActionConcurrencyError, _simple(status.HTTP_409_CONFLICT, "concurrent_modification")
    )
    # A runtime with no browser worker configured simply has no browser
    # capability. 503 rather than 500: nothing is broken, the capability is
    # absent, and no side effect was attempted.
    app.add_exception_handler(
        BrowserWorkerNotConfiguredError,
        _simple(status.HTTP_503_SERVICE_UNAVAILABLE, "browser_worker_not_configured"),
    )
    app.add_exception_handler(
        BrowserExecutionNotSupportedError,
        _simple(status.HTTP_409_CONFLICT, "browser_execution_not_supported"),
    )
    # A stored proposal the booking tool cannot parse is never executed on a
    # best-effort reading of the parts that did parse.
    app.add_exception_handler(
        BookingProposalError, _simple(status.HTTP_409_CONFLICT, "invalid_booking_proposal")
    )
    app.add_exception_handler(
        ActionAlreadyOpenError, _simple(status.HTTP_409_CONFLICT, "action_already_open")
    )
    app.add_exception_handler(
        TaskKindMismatchError, _simple(status.HTTP_409_CONFLICT, "task_kind_mismatch")
    )
    app.add_exception_handler(
        BookingSlotUnavailableError,
        _simple(status.HTTP_409_CONFLICT, "booking_slot_unavailable"),
    )
    app.add_exception_handler(
        BrowserObservationError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "browser_observation_failed",
            "The appointment site could not be read.",
        ),
    )
    # Worker reachability failures outside a dispatch (handshake, discovery).
    # The not-configured subclass keeps its own handler: Starlette resolves
    # handlers by the exception's MRO, most specific first.
    app.add_exception_handler(
        BrowserWorkerError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "browser_worker_unavailable",
            "The browser worker is unavailable.",
        ),
    )
    app.add_exception_handler(
        TaskHasUnresolvedActionError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "task_has_unresolved_action",
            "A booking for this task may already exist; resolve it first.",
        ),
    )
    app.add_exception_handler(
        TaskAlreadyBookedError,
        _fixed(status.HTTP_409_CONFLICT, "task_already_booked", "The booking for this task is confirmed."),
    )
    app.add_exception_handler(
        BookingCriteriaError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "invalid_booking_criteria",
            "The booking constraints could not be applied.",
        ),
    )
    app.add_exception_handler(
        BookingCriteriaMismatchError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "booking_criteria_mismatch",
            "That appointment no longer matches the requested constraints.",
        ),
    )
    app.add_exception_handler(DestinationNotAllowedError, _destination_not_allowed)
    app.add_exception_handler(AnswerNotGroundedError, _answer_not_grounded)
    app.add_exception_handler(
        InspectionProposalError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "invalid_inspection_proposal",
            "The stored inspection proposal is not valid.",
        ),
    )
    app.add_exception_handler(
        PublicInspectionNotConfiguredError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "public_inspection_not_configured",
            "Public page inspection is not configured.",
        ),
    )
    app.add_exception_handler(
        ObservationNotAvailableError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "observation_not_available",
            "There is no stored page observation for that action.",
        ),
    )
    app.add_exception_handler(
        StaleObservationError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "stale_observation",
            "That observation is no longer the current one.",
        ),
    )
    # --- Milestone 7b public research ------------------------------------
    app.add_exception_handler(
        ResearchNotConfiguredError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "research_not_configured",
            "Public web research is not configured.",
        ),
    )
    app.add_exception_handler(
        ResearchGrantNotFoundError,
        _fixed(
            status.HTTP_404_NOT_FOUND,
            "research_grant_not_found",
            "There is no research scope for that task.",
        ),
    )
    app.add_exception_handler(
        ResearchGrantNotUsableError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "research_grant_not_usable",
            "That research scope does not authorise anything right now.",
            "reason",
        ),
    )
    app.add_exception_handler(
        ResearchStepRefusedError,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "research_step_refused",
            "That research step was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        ProfileRefusal,
        # One stable code, one machine-readable reason, and never a path: a
        # profile refusal must not be the thing that tells a caller where the
        # profile directory is, or whether one exists.
        _reasoned(
            status.HTTP_409_CONFLICT,
            "browser_profile_refused",
            "That browser profile operation was refused.",
            "code",
        ),
    )
    app.add_exception_handler(DesktopRefusal, _desktop_refused)
    app.add_exception_handler(DesktopActionError, _desktop_action_refused)
    app.add_exception_handler(
        TakeoverRefusal,
        # Milestone 8a S2. Never page text, never a title, never a URL: a
        # takeover refusal carries a stable code and nothing page-derived.
        _reasoned(
            status.HTTP_409_CONFLICT,
            "login_attempt_refused",
            "That login takeover operation was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        ResearchRefusal,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "research_step_refused",
            "That research input was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        ResearchBudgetExhaustedError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "research_budget_exhausted",
            "This research task has reached one of its limits.",
            "limit",
        ),
    )
    app.add_exception_handler(
        ResearchStepInFlightError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "research_step_in_flight",
            "A research step for this task has not finished yet.",
        ),
    )
    app.add_exception_handler(
        ResearchSessionUnavailableError,
        _reasoned(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "research_session_unavailable",
            "The research browser session is not available.",
            "code",
        ),
    )
    app.add_exception_handler(
        ResearchAnswerAlreadyRecordedError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "research_answer_already_recorded",
            "This research task already has a recorded answer.",
        ),
    )
    app.add_exception_handler(
        ResearchAnswerNotGroundedError,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "research_answer_not_grounded",
            "The answer is not supported by what this task observed.",
            "code",
        ),
    )
    app.add_exception_handler(
        SearchFailedError,
        _reasoned(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "research_search_failed",
            "The public search could not be completed.",
            "code",
        ),
    )
    # --- Milestone 8a S3 authenticated account reading --------------------
    # Stable codes and reasons only: never page text, a title, a URL, a
    # profile path, an account identity or a provider's words.
    app.add_exception_handler(
        AuthenticatedReadNotConfiguredError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "authenticated_not_configured",
            "Authenticated account reading is not configured.",
        ),
    )
    app.add_exception_handler(
        AuthenticatedProfileUnavailableError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "authenticated_profile_unavailable",
            "That account profile cannot be read right now.",
            "code",
        ),
    )
    app.add_exception_handler(
        AuthenticatedGrantNotFoundError,
        _fixed(
            status.HTTP_404_NOT_FOUND,
            "authenticated_grant_not_found",
            "There is no account-reading scope for that task.",
        ),
    )
    app.add_exception_handler(
        AuthenticatedGrantNotUsableError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "authenticated_grant_not_usable",
            "That account-reading scope does not authorise anything right now.",
            "reason",
        ),
    )
    app.add_exception_handler(
        AuthenticatedStepRefusedError,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "authenticated_step_refused",
            "That account-reading step was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        AuthenticatedRefusal,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "authenticated_step_refused",
            "That account-reading input was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        AuthenticatedBudgetExhaustedError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "authenticated_budget_exhausted",
            "This account-reading task has reached one of its limits.",
            "limit",
        ),
    )
    app.add_exception_handler(
        AuthenticatedStepInFlightError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "authenticated_step_in_flight",
            "An account-reading step for this task has not finished yet.",
        ),
    )
    app.add_exception_handler(
        AuthenticatedAnswerAlreadyRecordedError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "authenticated_answer_already_recorded",
            "This account-reading task already has a recorded answer.",
        ),
    )
    # --- Milestone 8b S5 form planning and exact disclosure approval -------
    # A refused proposal is a 422; a *state* that no longer allows the approval
    # (the account changed, a saved value changed, the form was replaced) is a
    # 409. Codes only -- never a label, a preview, a value or a manifest.
    async def form_prepare_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, FormPrepareRefusal)
        stale = exc.code in FORM_PREPARE_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="form_prepare_state_changed" if stale else "form_prepare_refused",
                message="That form preparation cannot go ahead."
                if stale
                else "That form preparation was refused.",
                reason=exc.code,
            ),
        )

    app.add_exception_handler(FormPrepareRefusal, form_prepare_refused)

    # --- Milestone 9 S2 desktop disclosure --------------------------------------
    # Codes only -- never a window title, a question, desktop text, a quote or an answer.
    async def desktop_disclosure_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DesktopDisclosureRefusal)
        stale = exc.code in DESKTOP_DISCLOSURE_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="desktop_disclosure_state_changed" if stale else "desktop_disclosure_refused",
                message="That desktop disclosure is no longer available."
                if stale
                else "That desktop request was refused.",
                reason=exc.code,
            ),
        )

    app.add_exception_handler(DesktopDisclosureRefusal, desktop_disclosure_refused)

    # --- Milestone 9 S4 desktop action planning -----------------------------------
    # Codes only -- never a window title, an objective, a candidate value or a proposed action's names.
    async def desktop_plan_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DesktopPlanRefusal)
        stale = exc.code in DESKTOP_PLAN_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="desktop_plan_state_changed" if stale else "desktop_plan_refused",
                message="That desktop action plan is no longer available." if stale else "That desktop plan request was refused.",
                reason=exc.code,
            ),
        )

    app.add_exception_handler(DesktopPlanRefusal, desktop_plan_refused)

    # --- Milestone 9 S5 scoped visual fallback --------------------------------------
    # Codes only -- never a window title, a purpose string, a candidate label or pixels.
    async def desktop_vision_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DesktopVisionRefusal)
        stale = exc.code in DESKTOP_VISION_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="desktop_vision_state_changed" if stale else "desktop_vision_refused",
                message="That desktop visual fallback is no longer available." if stale else "That desktop visual fallback request was refused.",
                reason=exc.code,
            ),
        )

    app.add_exception_handler(DesktopVisionRefusal, desktop_vision_refused)

    # --- Milestone 10 S1 approved documents ------------------------------------------
    # Codes only -- never a path, a file name, a label, document text, a purpose or a quote.
    async def document_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DocumentRefusal)
        stale = exc.code in DOCUMENT_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="document_state_changed" if stale else "document_refused",
                message="That document is no longer available as approved." if stale else "That document request was refused.",
                reason=exc.code,
            ),
        )

    app.add_exception_handler(DocumentRefusal, document_refused)

    # --- Milestone 10 S2 controlled downloads and the cross-executor effect lock ------
    async def transfer_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, TransferRefusal)
        stale = exc.code in TRANSFER_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="transfer_state_changed" if stale else "transfer_refused",
                message="That download can no longer go ahead as approved." if stale else "That download request was refused.",
                reason=exc.code,
            ),
        )

    async def effect_locked(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, EffectLockedError)
        return _error(
            status.HTTP_409_CONFLICT,
            ErrorDetail(
                code="effect_locked",
                message="An earlier action with the same effect is unresolved. Reconcile it first; nothing was done.",
                reason=exc.reason,
            ),
        )

    async def project_refused(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProjectRefusal)
        stale = exc.code in PROJECT_STATE_CODES
        return _error(
            status.HTTP_409_CONFLICT if stale else status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(
                code="project_state_changed" if stale else "project_refused",
                message="That project run can no longer go ahead as approved." if stale else "That project request was refused.",
                reason=exc.code,
            ),
        )

    app.add_exception_handler(TransferRefusal, transfer_refused)
    app.add_exception_handler(ProjectRefusal, project_refused)
    app.add_exception_handler(EffectLockedError, effect_locked)
    app.add_exception_handler(
        ProtectedValueRefusal,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "protected_value_refused",
            "That saved detail was refused.",
            "code",
        ),
    )
    app.add_exception_handler(StaleTaskRevisionError, _stale_task_revision)
    app.add_exception_handler(StaleActionRevisionError, _stale_action_revision)
    app.add_exception_handler(ApprovalNotUsableError, _approval_not_usable)
