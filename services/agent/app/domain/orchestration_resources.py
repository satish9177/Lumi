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
}

#: Milestone 12 S2: resource kinds a trusted action outside the planner loop may register directly (never
#: minted by a capability's own success). Each entry here needs its own review before being added -- these
#: are the only two ways a resource may exist without ever having been produced by `_commit_step`/`resume`.
REGISTERABLE_RESOURCE_KINDS: Final[frozenset[str]] = frozenset({"document_ref", "public_url_ref"})

_REF_PATTERN: Final = re.compile(r"^r[1-9][0-9]{0,5}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

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
RESOURCE_STATE_CODES: Final = frozenset({"resource_not_found", "resource_consumed", "resource_expired"})


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
    "MAX_RESOURCES_PER_STEP",
    "MAX_SAFE_LABEL_CHARS",
    "PRIVACY_CLASSES",
    "REGISTERABLE_RESOURCE_KINDS",
    "RESOURCE_KINDS",
    "RESOURCE_STATE_CODES",
    "OrchestrationResourceRefusal",
    "ResourceOutputSpec",
    "next_ref",
    "safe_label",
    "trusted_input_label",
    "validate_ref",
    "validate_resource_refs",
]
