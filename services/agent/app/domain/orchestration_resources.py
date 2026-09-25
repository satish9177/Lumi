"""Milestone 12 S1: the trusted orchestration resource-ref registry -- vocabulary and validation only.

`general planning != general authority`, applied one level below capability selection: a planner may cite an
opaque ref (`r3`) that the controller itself minted, but citing it is a request to use it, never authority to
use it. This module holds the closed resource-kind enum, the closed capability x resource-kind compatibility
matrix, and pure validation. It holds no I/O and no tool of its own -- `app/repositories/orchestration_resources.py`
holds the SQL, `app/services/orchestration.py` decides when to mint or resolve one.

Two invariants this module exists to make true by construction, not by convention:

* **The model never mints a ref.** A ref is a plain opaque string (`r1`, `r2`, ...) the controller allocates
  when a step succeeds; nothing here parses a model-supplied ref as anything but a lookup key into an
  already-minted, already-owned row.
* **Visibility is not authority.** `CAPABILITY_RESOURCE_REQUIREMENTS` is a closed, per-capability, ordered
  tuple of the exact resource kinds that capability accepts as input. Every capability this runtime has
  composed as of Milestone 12 S1 (`public_research`, `project_status`, `project_start`) requires the empty
  tuple: citing ANY resource against any of them is refused, even a resource the citing orchestration freshly
  and legitimately owns. A later slice extends one capability's own entry when, and only when, it reviews
  that capability's resource-based composition.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from app.domain.orchestration import OrchestrationRefusal

MAX_SAFE_LABEL_CHARS: Final = 200
MAX_RESOURCES_PER_STEP: Final = 4

#: The closed resource-kind vocabulary, spelled identically to migration `0023` and `app/db/tables.py`
#: (pinned by `tests/test_orchestration_resources_domain.py`, since Python and the migration cannot share a
#: source file). Each kind already names a real M1-M10 input/output class
#: (`src/shared/agent-capabilities.ts`'s `inputClasses`/`outputClasses`); a kind gains a producer or consumer
#: only in the slice that reviews the matching capability composition.
RESOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "public_url_ref", "research_result_ref",
        "account_context_ref", "account_result_ref",
        "document_ref", "document_result_ref", "transfer_ref",
        "desktop_target_ref", "desktop_snapshot_ref", "desktop_result_ref",
        "app_ref", "project_ref", "project_status_ref",
        "form_target_ref", "form_result_ref", "workflow_ref",
    }
)

PRIVACY_CLASSES: Final[frozenset[str]] = frozenset({"public", "private", "none"})


@dataclass(frozen=True, slots=True)
class ResourceOutputSpec:
    """What a capability's own successful step mints. Controller data, never model-authored.

    `needs_backing_id`: whether the caller (Electron main, which already performed the real action through
    that capability's own existing method) must supply the new resource's backing id -- e.g. `document_read`
    must say which `document_id` its own extraction produced. The model never supplies this; it travels only
    from main's own trusted call into `advance()`, exactly like `task_id`/`resolved_summary` already do.
    """

    kind: str
    privacy_class: str
    single_use: bool = False
    needs_backing_id: bool = False

    def __post_init__(self) -> None:
        assert self.kind in RESOURCE_KINDS, f"unknown resource kind {self.kind!r}"
        assert self.privacy_class in PRIVACY_CLASSES, f"unknown privacy class {self.privacy_class!r}"


#: capability_id -> what its own successful step mints, if anything. Only capabilities this runtime has
#: actually composed (`app/domain/orchestration.py`'s `COMPOSED_CAPABILITY_IDS`) may appear here; a capability
#: absent from this dict mints no resource when it succeeds.
CAPABILITY_OUTPUT_RESOURCE: Final[dict[str, ResourceOutputSpec]] = {
    "public_research": ResourceOutputSpec(kind="research_result_ref", privacy_class="public"),
    "project_status": ResourceOutputSpec(kind="project_status_ref", privacy_class="none"),
    "project_start": ResourceOutputSpec(kind="project_status_ref", privacy_class="none"),
    #: Milestone 12 S2. Extracted text is `document_private` (`app/domain/documents.py`); the resource
    #: itself carries no text, only a controller-authored template label -- see `safe_label()`. Its backing
    #: id is the new `document_id` `DocumentService.extract()` produced, supplied by main.
    "document_read": ResourceOutputSpec(kind="document_result_ref", privacy_class="private", needs_backing_id=True),
    #: Milestone 12 S3. `account_read`'s own answer is `account_private` (`app/domain/authenticated.py`); like
    #: `document_read`, the resource itself carries no account text, only a controller-authored template
    #: label. It needs no backing id: nothing this runtime has composed yet cites an `account_result_ref` as
    #: input, so there is nothing for a later step to resolve it back to.
    "account_read": ResourceOutputSpec(kind="account_result_ref", privacy_class="private"),
    #: Milestone 12 S4. `desktop_observe`'s bounded projection is `desktop_private`
    #: (`app/domain/desktop_disclosure.py`'s own classification for anything derived from a UIA
    #: observation); the resource carries only a controller-authored template label, never a node, a role or
    #: any observed text. No backing id: nothing this runtime composes yet cites a `desktop_result_ref` as
    #: input.
    "desktop_observe": ResourceOutputSpec(kind="desktop_result_ref", privacy_class="private"),
    #: `desktop_reason`'s own grounded answer is likewise private and template-only in the step summary --
    #: see the private-data-leak lesson `docs/reviews/milestone-12-s3.md` already applied to `account_read`,
    #: reapplied here in `app/services/orchestration.py`'s `_read_task_backed_resolution`.
    "desktop_reason": ResourceOutputSpec(kind="desktop_result_ref", privacy_class="private"),
    #: `desktop_safe_action` (focus/scroll) has no private content of its own -- matching
    #: `agent-capabilities.ts`'s `resultPrivacyClass: 'none'` for this capability.
    "desktop_safe_action": ResourceOutputSpec(kind="desktop_result_ref", privacy_class="none"),
    "launch_registered_app": ResourceOutputSpec(kind="desktop_result_ref", privacy_class="none"),
    #: Reuses `project_status_ref`, exactly like `project_status`/`project_start`: a stopped run's own
    #: status is queried the same way a running one's is.
    "project_stop": ResourceOutputSpec(kind="project_status_ref", privacy_class="none"),
}

#: capability_id -> the exact ordered resource kinds it accepts as input. Absent or empty means "accepts no
#: resource"; `advance()` refuses `resources_not_supported` for any non-empty `resources` list against such a
#: capability. Every capability composed as of Milestone 12 S1 requires zero resources -- this is what makes
#: "the planner can see ref r1" independent of "the planner may use r1 with project_status" provably true
#: today, before any consumer exists at all.
CAPABILITY_RESOURCE_REQUIREMENTS: Final[dict[str, tuple[str, ...]]] = {
    "public_research": (),
    "project_status": (),
    "project_start": (),
    "document_read": ("document_ref",),
    #: `document_compare`'s M11 catalog descriptor names `document_ref` in its coarse `inputClasses`, but its
    #: own description says "two ALREADY-READ documents" -- `compare_local()` needs post-extraction document
    #: ids, so this requires the *result* of two `document_read` steps, not two unread file references.
    "document_compare": ("document_result_ref", "document_result_ref"),
    #: Milestone 12 S3. The one account context this step reads under -- never chosen or minted by the
    #: planner (see `REGISTERABLE_RESOURCE_KINDS` below). Selecting `account_read` on a real `account_context_ref`
    #: is not itself approval: the linked `authenticated_read` task's own existing scope card still gates
    #: every read, exactly as a direct request would.
    "account_read": ("account_context_ref",),
    #: Milestone 12 S4. Every desktop capability names the one approved window it reads/reasons about/acts
    #: on -- the planner never sees or chooses a HWND, PID, process-creation time or native selector; this
    #: ref is the only thing it can cite. The runtime re-resolves the real `(worker_generation, surface_ref,
    #: surface_epoch)` identity from the ref's own opaque backing text every time and re-validates it fresh
    #: against the live desktop through the EXISTING, unchanged M9 S1-S3 calls -- a recreated or closed
    #: window fails there exactly as it already would for a direct request; nothing here weakens that.
    "desktop_observe": ("desktop_target_ref",),
    "desktop_reason": ("desktop_target_ref",),
    #: Focus or semantic scroll only -- matching the current catalog's own closed description for this
    #: capability, not M9 S4's bounded `SetValue`/`Select`/`Invoke` mutations, which this slice does not
    #: compose (`app/domain/orchestration.py`'s own `TASK_BACKED_CAPABILITY_IDS` never links a
    #: `set_control_value`/`select_control`/`invoke_control` task under any capability).
    "desktop_safe_action": ("desktop_target_ref",),
    #: The app ref names a registered application only; the exe path, argument and working directory stay
    #: inside `AppRegistry`, never visible here or to the planner.
    "launch_registered_app": ("app_ref",),
    #: The project ref names the one Lumi-owned supervised run a trusted action already confirmed exists;
    #: the runtime resolves it back to that run's own task id and stops only that job, via
    #: `ProjectService.stop()`'s own existing, unchanged ownership check.
    "project_stop": ("project_ref",),
}

#: Milestone 12 S2/S3: resource kinds a trusted action outside the planner loop may register directly (never
#: minted by a capability's own success). Each entry here needs its own review before being added -- these
#: are the only ways a resource may exist without ever having been produced by `_commit_step`/`resume`.
#: `account_context_ref` (S3): main registers one only after `AuthenticatedReadService.check_profile` confirms
#: the profile is signed in, not mid-takeover and not leased elsewhere -- the same check a direct
#: authenticated-read request already passes through; the model never creates or relabels one.
#: Milestone 12 S4 adds three more, each bound to trusted UI selection only:
#: `desktop_target_ref`: Electron main re-lists the live desktop and the runtime independently re-lists it
#: again itself before registering, both confirming the exact `(worker_generation, surface_ref,
#: surface_epoch)` the renderer named still resolves to a real, visible surface -- the renderer supplies an
#: opaque choice from that same listing, never a handle, PID or coordinate it invented.
#: `app_ref`: main confirms the id is one of the trusted, already-configured registered applications before
#: registering; `DesktopActionService.propose_launch` independently re-checks the registry again at dispatch.
#: `project_ref`: main confirms a Lumi-owned supervised run is currently alive before registering;
#: `ProjectService.stop()` independently re-checks ownership and phase again at dispatch.
REGISTERABLE_RESOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {"document_ref", "public_url_ref", "account_context_ref", "desktop_target_ref", "app_ref", "project_ref"}
)

_REF_PATTERN: Final = re.compile(r"^r[1-9][0-9]{0,5}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: Milestone 12 S4: `desktop_target_ref`'s model-invisible backing text -- `{worker_generation}|{surface_ref}
#: |{surface_epoch}`, spelled from the same closed shapes M9 already uses (`SURFACE_REF_PATTERN`,
#: a monotonic epoch). Stored in `backing_text` rather than `backing_id` because a desktop target's identity
#: is three fields, not one uuid, and `orchestration_resources` allows only one of the two backing columns to
#: be set at a time (migration `0024`'s `ck_orchestration_resources_backing_exclusive`). Never shown to the
#: planner -- only `app/services/orchestration.py`'s own trusted registration and dispatch code parses it.
DESKTOP_TARGET_BACKING_PATTERN: Final = re.compile(
    r"^(?P<worker_generation>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\|(?P<surface_ref>s(?:[1-9]|1[0-6]))\|(?P<surface_epoch>[1-9][0-9]{0,8})$"
)


@dataclass(frozen=True, slots=True)
class DesktopTargetBacking:
    worker_generation: str
    surface_ref: str
    surface_epoch: int


def encode_desktop_target_backing(*, worker_generation: str, surface_ref: str, surface_epoch: int) -> str:
    """Builds the opaque backing text a trusted registration stores. Re-parsed, never trusted verbatim, by
    `parse_desktop_target_backing` at every later use."""
    return f"{worker_generation}|{surface_ref}|{surface_epoch}"


def parse_desktop_target_backing(value: str) -> DesktopTargetBacking:
    """Strict re-parse of a `desktop_target_ref`'s own backing text. Anything outside the exact closed shape
    is refused, never guessed -- the same rule `parse_proposal` already applies to a persisted desktop
    action."""
    match = DESKTOP_TARGET_BACKING_PATTERN.fullmatch(value)
    if match is None:
        raise OrchestrationResourceRefusal("resources_invalid")
    return DesktopTargetBacking(
        worker_generation=match.group("worker_generation"),
        surface_ref=match.group("surface_ref"),
        surface_epoch=int(match.group("surface_epoch")),
    )

#: Resource refusals reuse `OrchestrationRefusal` rather than a second exception type, so
#: `app/api/errors.py`'s one existing `OrchestrationRefusal` handler already covers every code below -- no
#: second handler to keep in sync, and no risk of a resource refusal silently falling through to a generic
#: 500 because a caller only ever registered the orchestration one.
OrchestrationResourceRefusal = OrchestrationRefusal

#: State refusals (409): the world moved on (a ref does not/no longer resolve), not a malformed request --
#: mapped to 409 in `app/api/errors.py`, exactly like `orchestration.py`'s own `orchestration_not_found` /
#: `orchestration_expired`. `resources_not_supported` and `resource_kind_mismatch` are deterministic shape
#: mismatches against static capability metadata (like `orchestration.py`'s own `task_kind_mismatch` /
#: `task_id_not_allowed`), so they stay 422 by not appearing here.
#: `desktop_target_unavailable` (S4): the window a trusted registration named no longer resolves against a
#: fresh desktop listing (closed, recreated, or a different worker generation) -- the world moved on between
#: the renderer's own listing and main's registration call, exactly the same class of fact as a consumed or
#: expired resource, not a malformed request.
#: `project_run_not_live` (S4): the run a trusted registration named has already ended (or never was a
#: project run) by the time `register_resource` re-checks it -- the same class of fact, not a malformed
#: request.
RESOURCE_STATE_CODES: Final = frozenset(
    {"resource_not_found", "resource_consumed", "resource_expired", "desktop_target_unavailable", "project_run_not_live"}
)


def validate_ref(value: object) -> str:
    """A real opaque ref shape (`r1`, `r2`, ...) and nothing else -- never a UUID, a path, or a URL."""
    if not isinstance(value, str) or not _REF_PATTERN.fullmatch(value):
        raise OrchestrationResourceRefusal("resources_invalid")
    return value


def validate_resource_refs(value: object) -> tuple[str, ...]:
    """The wire-level `resources` field: `None`/absent means none cited. Bounded, deduplicated, ref-shaped."""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_RESOURCES_PER_STEP:
        raise OrchestrationResourceRefusal("resources_invalid")
    refs = tuple(validate_ref(item) for item in value)
    if len(set(refs)) != len(refs):
        raise OrchestrationResourceRefusal("resources_invalid")
    return refs


def safe_label(value: str) -> str:
    """A controller-authored label. Bounded and control-character-stripped like `bounded_summary`, but this
    function does not, by itself, make arbitrary text safe -- callers must only ever pass template text they
    authored, never a document/account/desktop/page's own content. See `CAPABILITY_OUTPUT_RESOURCE` callers."""
    text = " ".join(unicodedata.normalize("NFC", value).split())
    text = _CONTROL.sub(" ", text)
    if len(text) <= MAX_SAFE_LABEL_CHARS:
        return text
    return text[: MAX_SAFE_LABEL_CHARS - 1] + "…"


def trusted_input_label(value: str) -> str:
    """Bounds and sanitises a label the same way `safe_label` does, for the different, narrower case of
    `REGISTERABLE_RESOURCE_KINDS`: a resource the user themselves made available through a trusted action
    (picking an already-approved file, typing a URL Lumi already checked) -- never a capability's own result
    content. Echoing the user's own already-seen file name or URL back to the planner discloses nothing they
    did not already provide; this is categorically different from `safe_label`'s template-only rule for a
    resource a capability's own execution produced."""
    return safe_label(value)


def next_ref(existing_count: int) -> str:
    """The next opaque ref for an orchestration that already has `existing_count` resources. Controller-only:
    nothing here is ever handed a model- or renderer-supplied number."""
    return f"r{existing_count + 1}"


__all__ = [
    "CAPABILITY_OUTPUT_RESOURCE",
    "CAPABILITY_RESOURCE_REQUIREMENTS",
    "DESKTOP_TARGET_BACKING_PATTERN",
    "MAX_RESOURCES_PER_STEP",
    "MAX_SAFE_LABEL_CHARS",
    "PRIVACY_CLASSES",
    "REGISTERABLE_RESOURCE_KINDS",
    "RESOURCE_KINDS",
    "RESOURCE_STATE_CODES",
    "DesktopTargetBacking",
    "OrchestrationResourceRefusal",
    "ResourceOutputSpec",
    "encode_desktop_target_backing",
    "next_ref",
    "parse_desktop_target_backing",
    "safe_label",
    "trusted_input_label",
    "validate_ref",
    "validate_resource_refs",
]
