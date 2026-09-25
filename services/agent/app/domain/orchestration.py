"""Milestone 11 S2: durable read-only orchestration -- the graph, not the planner.

`general planning != general authority`. This module is the closed vocabulary and state machine an
orchestration graph obeys; it holds no tool of its own. Choosing a capability id means only "the next
useful step is this one" -- it is never authority to read a path, open an address, or execute an effect.
Every capability's own approval, grant and disclosure boundary (Milestone 1-10) stays authoritative; the
orchestrator only records which one was chosen and what its bounded result was.

Two kinds of step:

* **Task-backed** (`public_research` today): the caller (Electron main, which alone drives model calls and
  knows configured providers) creates the child task through that capability's own existing entry point --
  exactly the same one a direct request uses -- and hands this module only the resulting `task_id` to link.
  Its own approval/grant/disclosure requirements are shown to the user exactly as they would be outside an
  orchestration; nothing here skips them.
* **Synchronous** (`project_status` today): a pure read with no side effect and no new task. The caller
  reads it through the capability's own existing method and hands this module only the bounded summary to
  record.

`COMPOSED_CAPABILITY_IDS` is a small, honestly-scoped subset of the full Milestone 11 S1 catalog
(`src/shared/agent-capabilities.ts`). Every id in the full catalog is a real, already-reviewed capability;
choosing one this runtime has not yet composed is refused (`capability_not_composed`), never silently
mis-executed and never aggregated into another capability's authority.
"""

import re
import unicodedata
from enum import StrEnum
from typing import Final

from app.domain.authenticated import AUTHENTICATED_READ_TASK_TYPE
from app.domain.desktop_actions import DESKTOP_ACTION_TASK_TYPE
from app.domain.desktop_disclosure import DESKTOP_READ_TASK_TYPE

MAX_OBJECTIVE_CHARS: Final = 500
MAX_RESULT_SUMMARY_CHARS: Final = 600
ORCHESTRATION_TTL_SECONDS: Final = 30 * 60

#: Initial, conservative budgets (Milestone 11's plan doc). Hardened further at the Milestone 11 S5 audit;
#: exhausting one pauses the orchestration -- it never silently widens the limit.
MAX_STEPS: Final = 20
MAX_CHILD_TASKS: Final = 10
MAX_PLANNER_CALLS: Final = 20

#: The full closed capability catalog, spelled identically to `src/shared/agent-capabilities.ts`'s
#: `AGENT_CAPABILITY_IDS` and to the `0021` migration's own list (both pinned against this one by
#: `tests/test_orchestration_domain.py`, since Python and TypeScript cannot share a source file).
CATALOG_CAPABILITY_IDS: Final[frozenset[str]] = frozenset(
    {
        "public_research", "inspect_public_page", "account_read", "document_read", "document_compare",
        "download_document", "place_downloaded_file", "desktop_observe", "desktop_reason",
        "desktop_safe_action", "launch_registered_app", "project_status", "project_start", "project_stop",
        "form_prepare", "workflow_prepare",
    }
)

#: Task-backed: the caller creates/owns the child task through its own existing capability boundary and
#: links it here. `EXPECTED_TASK_TYPE` is the `tasks.request.type` a linked task must actually have --
#: checked, never trusted, so a caller cannot link an unrelated task under a mismatched capability id.
#: `project_start` is Milestone 11 S3's one composed effectful capability: its own effect key
#: (`app/domain/effects.py`'s `PROJECT_RUN`, a global-tier kind) and cross-executor lock apply exactly as
#: they do to a direct request, because starting still happens through `ProjectService.start()` itself --
#: this module never claims or checks an effect key of its own.
#: `account_read` (Milestone 12 S3) links an `authenticated_read` task exactly the same way `public_research`
#: links a `public_research` one: Electron main creates it through `AuthenticatedReadService`'s own existing
#: boundary (its own scope card, grant and disclosure recipient, unchanged), and this module only reads that
#: task's own current resolution -- see `_read_task_backed_resolution` in `app/services/orchestration.py`.
#: `desktop_reason` (Milestone 12 S4) links a `desktop_read` task exactly the same way: Electron main creates
#: it through `DesktopDisclosureService.create()`'s own existing disclosure card, unchanged. `desktop_safe_action`
#: and `launch_registered_app` both link a `desktop_action` task -- the SAME task type `DesktopActionService`
#: already uses for every desktop effect, S3 and S4 alike, so `_read_task_backed_resolution` additionally
#: checks the linked task's own `operation` field (never trusted from `EXPECTED_TASK_TYPE` alone) to refuse a
#: `set_control_value`/`select_control`/`invoke_control` task ever resolving as either of these two.
TASK_BACKED_CAPABILITY_IDS: Final[frozenset[str]] = frozenset(
    {"public_research", "project_start", "account_read", "desktop_reason", "desktop_safe_action", "launch_registered_app"}
)
EXPECTED_TASK_TYPE: Final[dict[str, str]] = {
    "public_research": "public_research",
    "project_start": "project_run_task",
    "account_read": AUTHENTICATED_READ_TASK_TYPE,
    "desktop_reason": DESKTOP_READ_TASK_TYPE,
    "desktop_safe_action": DESKTOP_ACTION_TASK_TYPE,
    "launch_registered_app": DESKTOP_ACTION_TASK_TYPE,
}

#: `desktop_safe_action`/`launch_registered_app` share `DESKTOP_ACTION_TASK_TYPE` with the S4 mutation
#: operations that stay excluded from orchestration entirely; this is the second check
#: `_read_task_backed_resolution` makes (`task.request["operation"]`) before ever trusting a linked
#: `desktop_action` task as one of these two capabilities' own.
DESKTOP_SAFE_ACTION_OPERATIONS: Final[frozenset[str]] = frozenset({"focus_surface", "scroll_control"})
DESKTOP_LAUNCH_OPERATIONS: Final[frozenset[str]] = frozenset({"launch_app"})

#: Synchronous: a pure read, resolved by the caller with no new task and no approval. `document_read` and
#: `document_compare` (Milestone 12 S2) join this set: the caller (Electron main) has already extracted or
#: compared through `DocumentService`'s own existing, no-new-approval methods before calling `advance()`;
#: this module only records the bounded, controller-authored fact and, for `document_read`, mints the
#: resulting `document_result_ref` (see `app/domain/orchestration_resources.py`). `desktop_observe` and
#: `project_stop` (Milestone 12 S4) join this set for the same reason: main already performed the real,
#: read-only observation or the real stop through that capability's own existing, no-new-approval method
#: before calling `advance()`.
SYNCHRONOUS_CAPABILITY_IDS: Final[frozenset[str]] = frozenset(
    {"project_status", "document_read", "document_compare", "desktop_observe", "project_stop"}
)

#: Milestone 12 S2. The M11 loop guard ("re-choosing an already-succeeded capability is not progress") assumed
#: every composed capability answers exactly one thing per orchestration -- true for `public_research`/
#: `project_status`/`project_start`, false for `document_read` (up to `MAX_FILES_PER_TASK` files) and
#: `document_compare` (a person may reasonably compare more than one pair). These two are exempted from that
#: guard; `MAX_STEPS` still bounds the total regardless, exactly as it already bounds every other capability.
#: `desktop_safe_action` (Milestone 12 S4) joins them: focusing a window and then scrolling it, or scrolling
#: it more than once, is ordinary use of a read-only-effect capability, and each individual action still
#: needs its own fresh freshness check and its own exact approval regardless of how many times this
#: capability is chosen.
REPEATABLE_CAPABILITY_IDS: Final[frozenset[str]] = frozenset({"document_read", "document_compare", "desktop_safe_action"})

#: What this runtime can actually execute today. A strict, honestly-scoped subset of the full catalog.
COMPOSED_CAPABILITY_IDS: Final[frozenset[str]] = TASK_BACKED_CAPABILITY_IDS | SYNCHRONOUS_CAPABILITY_IDS


class OrchestrationStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


TERMINAL_ORCHESTRATION_STATUSES: Final = frozenset(
    {OrchestrationStatus.SUCCEEDED, OrchestrationStatus.FAILED, OrchestrationStatus.STOPPED}
)


class StepStatus(StrEnum):
    #: A task-backed step whose child task exists but has not yet produced a usable result and needs no
    #: further human action Lumi knows about right now (the ordinary case is `AWAITING_APPROVAL`; `PENDING`
    #: covers the narrow window where the task exists but its resolution has not been checked yet).
    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


UNRESOLVED_STEP_STATUSES: Final = frozenset({StepStatus.PENDING, StepStatus.AWAITING_APPROVAL})

#: Closed pause-reason vocabulary. Every reason maps to a controller decision, never a model's.
#: `manual_handoff_required`: a human must act outside Lumi (a login, a CAPTCHA-guarded login, an
#: unsupported control) before the orchestration can continue. Milestone 12 S3 makes this reachable for the
#: first time, through `account_read`'s linked `authenticated_read` task: its own existing
#: `login_required`/`account_changed`/`account_identity_unknown`/`left_site_scope` pauses (all detected by
#: `AuthenticatedReadService` already, unchanged) map onto this one orchestration-level reason. A dedicated
#: CAPTCHA signal is deliberately not invented here: a CAPTCHA-guarded sign-in already shows a credential
#: surface (a password field, at minimum), which `login_required` already, and over-inclusively, catches --
#: see `app/browser/credential_signals.py`.
#: `outcome_unknown`: a linked capability's own effect is unresolved (a research step interrupted
#: mid-flight; a project run whose own `RunView.phase` is itself `outcome_unknown`).
PAUSE_REASONS: Final = frozenset(
    {
        "approval_required", "budget_exhausted", "loop_detected", "capability_unavailable",
        "manual_handoff_required", "outcome_unknown",
    }
)

#: Refusals that are *state* (the world moved on), not a malformed request -- mapped to 409, not 422.
STATE_CODES: Final = frozenset(
    {
        "orchestration_not_found",
        "orchestration_not_active",
        "orchestration_expired",
        "step_already_resolved",
        "step_not_pending",
        "child_task_already_linked",
    }
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class OrchestrationRefusal(ValueError):
    """A refused orchestration request or state. `code` is stable and never carries a value, text or a path."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The orchestration request was refused ({code}).")
        self.code = code


def validate_objective(value: object) -> str:
    if not isinstance(value, str):
        raise OrchestrationRefusal("objective_invalid")
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text or len(text) > MAX_OBJECTIVE_CHARS or _CONTROL.search(text):
        raise OrchestrationRefusal("objective_invalid")
    return text


def validate_capability_id(value: object) -> str:
    """A real catalog id, closed and exact -- never a near-miss spelling, never a model invention."""
    if not isinstance(value, str) or value not in CATALOG_CAPABILITY_IDS:
        raise OrchestrationRefusal("capability_unknown")
    return value


def bounded_summary(value: str) -> str:
    """A controller-authored result summary, plain text, bounded. Never raw private evidence verbatim."""
    text = " ".join(unicodedata.normalize("NFC", value).split())
    text = _CONTROL.sub(" ", text)
    if len(text) <= MAX_RESULT_SUMMARY_CHARS:
        return text
    return text[: MAX_RESULT_SUMMARY_CHARS - 1] + "…"


def result_handle(output_class: str, sequence: int) -> str:
    """`research_result:3`-shaped, matching the plan doc's `research_result:r1` family in spirit: a
    controller-authored, opaque reference a planner may cite -- never a path, an id another task minted, or
    anything a model chose the shape of."""
    return f"{output_class}:{sequence}"


__all__ = [
    "CATALOG_CAPABILITY_IDS",
    "COMPOSED_CAPABILITY_IDS",
    "DESKTOP_LAUNCH_OPERATIONS",
    "DESKTOP_SAFE_ACTION_OPERATIONS",
    "EXPECTED_TASK_TYPE",
    "MAX_CHILD_TASKS",
    "MAX_OBJECTIVE_CHARS",
    "MAX_PLANNER_CALLS",
    "MAX_RESULT_SUMMARY_CHARS",
    "MAX_STEPS",
    "ORCHESTRATION_TTL_SECONDS",
    "PAUSE_REASONS",
    "REPEATABLE_CAPABILITY_IDS",
    "STATE_CODES",
    "SYNCHRONOUS_CAPABILITY_IDS",
    "TASK_BACKED_CAPABILITY_IDS",
    "TERMINAL_ORCHESTRATION_STATUSES",
    "UNRESOLVED_STEP_STATUSES",
    "OrchestrationRefusal",
    "OrchestrationStatus",
    "StepStatus",
    "bounded_summary",
    "result_handle",
    "validate_capability_id",
    "validate_objective",
]
