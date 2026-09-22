"""Milestone 9 S4: bounded desktop-action planning.

This module is deliberately the narrowest possible extension of S2's disclosure pattern
(`app.domain.desktop_disclosure`), reused here almost entirely: the same grant machinery, the same
redacted, bounded projection, the same "one provider, one attempt, no failover, no image" policy. What
S4 changes is what the provider is allowed to say back.

```text
one exact, FRESH observation (S1, desktop_private, persisted)
   -> trusted card: ONE provider, THIS snapshot, redacted, once, plus any values the person typed
   -> trusted click: DesktopPlanScope grant (task_grants, kind=desktop_action_plan)
   -> claim: grant ACTIVE -> COMPLETED and a UNIQUE desktop_action_plans row, in one transaction
   -> the redacted projection AND value descriptors (never raw values) go to Electron main
   -> exactly ONE provider attempt; no failover, no retry, no image
   -> a closed, bounded action proposal is recorded (or the failure, or OUTCOME_UNKNOWN)
```

**Disclosure is not execution authority.** Recording a plan's proposed action here does not run
anything. `DesktopActionService.propose_from_plan` still builds an ordinary `DESKTOP_SET_VALUE` /
`DESKTOP_SELECT` / `DESKTOP_INVOKE` action from it, which still needs its own separate, exact approval
-- a second trusted click, on a second trusted card -- before any effect exists. The model never
authorizes execution; the renderer never decides the operation, target, value or effect.

**What the model may propose.** Exactly one action, from a closed set:

* `invoke(controlRef)`
* `set_value(controlRef, valueRef)`
* `select(containerRef, optionRef)`

A `valueRef` names one of the person's own typed candidate values (never a value the model itself
wrote); the model sees only its `classification` and `length`, never its text. There is no field for a
raw value, a coordinate, a native id, a risk tier, a provider or an approval -- exactly as S2's read
result has no field for an operation. `extra="forbid"` refuses a reply that tries.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.desktop.protocol import CONTROL_REF_PATTERN, SURFACE_REF_PATTERN
from app.domain.authenticated import Recipient
from app.domain.desktop_disclosure import (
    ALLOWED_FIELDS,
    DESKTOP_PRIVATE,
    MAX_PROJECTED_NODES,
    MAX_PROJECTED_TEXT_BYTES,
    REDACTION_POLICY,
    DisplayTarget,
    Projection,
)
from app.domain.digest import canonical_json

DESKTOP_ACTION_PLAN_TASK_TYPE: Final = "desktop_action_planning"
DESKTOP_PLAN_KIND: Final = "desktop_action_plan"
DESKTOP_PLAN_POLICY_VERSION: Final = "desktop-action-plan-v1"

#: A planning grant is short-lived, like S2's disclosure grant.
DEFAULT_PLAN_GRANT_TTL_SECONDS: Final = 10 * 60
#: S4 actions need a much fresher observation than S2's read-only reasoning (up to 10 minutes old):
#: an action mutates the application, so the snapshot it was planned against should still resemble the
#: live window. This bounds only whether a plan may be OFFERED and CLAIMED against that observation;
#: live target re-resolution immediately before the effect (in the worker) is mandatory regardless and
#: is the actual authority for what exists at effect time.
PLANNING_OBSERVATION_MAX_AGE_SECONDS: Final = 20
STALE_CLAIM_SECONDS: Final = 5 * 60

MAX_OBJECTIVE_CHARS: Final = 500
MAX_VALUES: Final = 4
MAX_VALUE_CHARS: Final = 500
MAX_VALUE_CLASSIFICATION_CHARS: Final = 32
MAX_MODEL_NAME: Final = 64

_DIGEST = r"^[0-9a-f]{64}$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VALUE_REF_PATTERN: Final = re.compile(r"^v([1-9]|10)$")
ValueRef = Annotated[str, Field(pattern=VALUE_REF_PATTERN.pattern)]
ControlRef = Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]

#: Stable, text-free failure codes recorded against a plan.
ERROR_MODEL_UNAVAILABLE: Final = "model_unavailable"
ERROR_INVALID_OUTPUT: Final = "invalid_output"
ERROR_ACTION_REFUSED: Final = "action_refused"
ERROR_RUNTIME_RESTART: Final = "runtime_restart"
ERROR_OBSERVATION_GONE: Final = "observation_unavailable"
PROVIDER_FAILURE_CODES: Final = frozenset({ERROR_MODEL_UNAVAILABLE, ERROR_INVALID_OUTPUT})

#: The CLOSED, reviewed set of Invoke labels this milestone accepts for the v1 `NAME_TOGGLE` effect.
#: This is an ALLOWLIST, not a denylist. An accessible label is attacker/application-controlled and
#: can never PROVE an effect harmless -- a real submit/payment/delete button can just as easily be
#: labelled "Continue" as "Submit", and can rename itself on press as an ordinary UX pattern, which is
#: exactly the postcondition `NAME_TOGGLE` looks for. Denying a list of dangerous-sounding words (the
#: previous shape of this check) is therefore never sufficient by itself: it protects only against a
#: button honest enough to use one of the listed words. Requiring the CURRENT label to match one of a
#: short, reviewed set of known non-destructive disclosure/expand-collapse toggles, and refusing
#: everything else -- including an entirely innocuous-looking label that just isn't on this list --
#: is the fail-closed shape the brief requires: "Initial S4 should support only reviewed
#: non-destructive effects with deterministic verification." Exact match only (trimmed,
#: case-insensitive) so a compound label ("Show details and submit") is not accepted just because it
#: contains an allowed phrase. English-centric; documented residual, like the S1 credential-name
#: heuristic.
SAFE_INVOKE_LABELS: Final = frozenset(
    {
        "show details", "hide details", "show more", "show less", "more details", "less details",
        "expand", "collapse", "expand details", "collapse details", "expand all", "collapse all",
    }
)

#: Found by the final M9 cross-slice audit: unlike `invoke_control`, `select_control` had no
#: equivalent of the allowlist above -- only a referential check that the refs exist. A
#: `SelectionItem.Select` is frequently activation, not mere highlighting: a browser/Electron
#: `<select>`-style control commonly fires an immediate change handler, and a custom ARIA
#: `listbox`/`role="option"` "quick pick" widget (a command palette, for instance) can treat
#: selecting an entry as running it -- exactly the class of risk that made Invoke's own label
#: allowlist necessary, applied here to the CONTAINER's role instead of the option's label,
#: because an option's own text is arbitrary application data (a filename, a search result), never
#: a small reviewed set of UI-chrome phrases the way a button's label can be. `combo_box` and
#: `radio_button` are classic closed-set value pickers (an ordinary form field), not a standalone
#: action list; a `list`/`pane`-rooted container -- where a command-palette-style widget lives --
#: is refused. This is additional, reviewed defence, not the only thing standing between a model's
#: proposal and an effect: `validate_planned_action` remains a referential check otherwise, and the
#: worker's own live re-derivation (exact match, no nearest-label fallback) is what actually makes
#: the approved target impossible to substitute.
SAFE_SELECT_CONTAINER_ROLES: Final = frozenset({"combo_box", "radio_button"})


class DesktopPlanRefusal(ValueError):
    """A refused desktop-plan input or state. `code` is stable and text-free."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The desktop action plan was refused ({code}).")
        self.code = code


STATE_CODES: Final = frozenset(
    {
        "grant_not_found", "grant_not_pending", "grant_not_active", "grant_expired", "grant_changed",
        "observation_unavailable", "observation_changed", "observation_stale", "plan_not_started",
        "plan_already_recorded", "wrong_recipient",
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---- candidate values: trusted, local, never from a model ------------------------------------------


class ValueDescriptor(_Frozen):
    """What the MODEL is shown for one of the person's typed candidate values: never the text itself."""

    value_ref: ValueRef
    #: A short label the PERSON chose for their own value (e.g. "search text"), never inferred from
    #: the desktop. Purely a hint; the model cannot use it to bypass anything.
    classification: str = Field(max_length=MAX_VALUE_CLASSIFICATION_CHARS)
    length: int = Field(ge=0, le=MAX_VALUE_CHARS)


class StoredValue(ValueDescriptor):
    """What the immutable grant scope holds: the descriptor plus the trusted raw text.

    This is the ONLY place S4 keeps a value at rest outside the worker RPC that finally writes it, and
    it is never copied into `desktop_action_plans`, a `DesktopProposal`'s dispatch row, an event or a
    log. `SetValueProposal` (the execution-time proposal, once a plan is translated into an ordinary
    ledger action) copies the raw text out of here once, the same way S3 already copies untrusted
    display text into a proposal for its trusted card.
    """

    raw: str = Field(max_length=MAX_VALUE_CHARS)

    @field_validator("raw")
    @classmethod
    def _plain(cls, value: str) -> str:
        if _CONTROL.search(value):
            raise ValueError("control characters are not allowed")
        return value

    def descriptor(self) -> ValueDescriptor:
        return ValueDescriptor(value_ref=self.value_ref, classification=self.classification, length=self.length)


# ---- the grant scope ---------------------------------------------------------------------------------


class DesktopPlanScope(_Frozen):
    """Exactly what one planning grant authorises: letting ONE named provider see the redacted,
    bounded projection of ONE exact observation plus the descriptors of the person's own candidate
    values, once, and propose back ONE closed action. It is not authority to run anything."""

    schema_version: Literal[1] = 1
    kind: Literal["desktop_action_plan"] = DESKTOP_PLAN_KIND
    policy_version: Literal["desktop-action-plan-v1"] = DESKTOP_PLAN_POLICY_VERSION
    observation_id: uuid.UUID
    snapshot_digest: str = Field(pattern=_DIGEST)
    observed_at: datetime
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=SURFACE_REF_PATTERN.pattern)
    surface_epoch: int = Field(ge=1)
    recipient: Recipient
    model: str = Field(pattern=_MODEL.pattern)
    objective: str = Field(max_length=MAX_OBJECTIVE_CHARS)
    allowed_fields: tuple[str, ...] = ALLOWED_FIELDS
    max_nodes: int = Field(default=MAX_PROJECTED_NODES, ge=1, le=MAX_PROJECTED_NODES)
    max_text_bytes: int = Field(default=MAX_PROJECTED_TEXT_BYTES, ge=1, le=MAX_PROJECTED_TEXT_BYTES)
    redaction_policy: Literal["identifier-redaction-v1"] = REDACTION_POLICY
    max_provider_calls: Literal[1] = 1
    failover: Literal["none"] = "none"
    classification: Literal["desktop_private"] = DESKTOP_PRIVATE
    display: DisplayTarget
    values: tuple[StoredValue, ...] = Field(default=(), max_length=MAX_VALUES)

    @field_validator("allowed_fields")
    @classmethod
    def _fixed_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ALLOWED_FIELDS:
            raise ValueError("the projected fields are a fixed, closed set")
        return value

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("values")
    @classmethod
    def _unique_refs(cls, value: tuple[StoredValue, ...]) -> tuple[StoredValue, ...]:
        refs = [item.value_ref for item in value]
        if len(refs) != len(set(refs)):
            raise ValueError("value refs must be unique")
        return value

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    def withheld(self) -> tuple[str, ...]:
        return tuple(item for item in (self.display.window_title, self.display.application_label) if item)

    def value_descriptors(self) -> tuple[ValueDescriptor, ...]:
        """What the provider is shown for the candidate values: never the raw text."""
        return tuple(item.descriptor() for item in self.values)

    def resolve_value(self, value_ref: str) -> str | None:
        for item in self.values:
            if item.value_ref == value_ref:
                return item.raw
        return None


# ---- the planned action --------------------------------------------------------------------------


class InvokeAction(_Frozen):
    schema_version: Literal[1] = 1
    action: Literal["invoke"] = "invoke"
    control_ref: ControlRef


class SetValueAction(_Frozen):
    schema_version: Literal[1] = 1
    action: Literal["set_value"] = "set_value"
    control_ref: ControlRef
    value_ref: ValueRef


class SelectAction(_Frozen):
    schema_version: Literal[1] = 1
    action: Literal["select"] = "select"
    container_ref: ControlRef
    option_ref: ControlRef


PlannedAction = InvokeAction | SetValueAction | SelectAction


def parse_planned_action(payload: object) -> PlannedAction:
    """Strictly read one provider action. Unknown keys, an unknown discriminator, a coordinate, a raw
    value, a risk tier, an approval or a provider name all refuse the whole reply -- there is nowhere
    to put any of them."""
    if not isinstance(payload, dict):
        raise DesktopPlanRefusal("result_malformed")
    action = payload.get("action")
    try:
        if action == "invoke":
            return InvokeAction.model_validate(payload)
        if action == "set_value":
            return SetValueAction.model_validate(payload)
        if action == "select":
            return SelectAction.model_validate(payload)
    except ValidationError:
        raise DesktopPlanRefusal("result_malformed") from None
    raise DesktopPlanRefusal("result_malformed")


def dump_planned_action(action: PlannedAction) -> dict[str, object]:
    return action.model_dump(mode="json")


def _is_reviewed_safe_invoke(node: dict[str, object]) -> bool:
    name = str(node.get("name") or "").strip().lower()
    return name in SAFE_INVOKE_LABELS


def _is_reviewed_safe_select_container(node: dict[str, object]) -> bool:
    role = str(node.get("role") or "").strip().lower()
    return role in SAFE_SELECT_CONTAINER_ROLES


def validate_planned_action(
    projection: Projection, scope: DesktopPlanScope, action: PlannedAction
) -> None:
    """Refuse an action the approved projection (and, for `set_value`, the approved values) does not
    support. This is a REFERENTIAL check only -- every ref the model named must exist in exactly the
    snapshot it was shown -- not a safety proof: identity, pattern availability, read-only state,
    sensitive-target refusal, container membership and human takeover are all re-verified again,
    independently, by the worker against the LIVE tree immediately before any effect, and none of
    those checks is skipped because this one passed.
    """
    if isinstance(action, InvokeAction):
        node = projection.node(action.control_ref)
        if node is None:
            raise DesktopPlanRefusal("unknown_control")
        if not _is_reviewed_safe_invoke(node):
            raise DesktopPlanRefusal("unsupported_or_unknown_effect")
    elif isinstance(action, SetValueAction):
        node = projection.node(action.control_ref)
        if node is None:
            raise DesktopPlanRefusal("unknown_control")
        if scope.resolve_value(action.value_ref) is None:
            raise DesktopPlanRefusal("unknown_value_ref")
    else:
        container = projection.node(action.container_ref)
        option = projection.node(action.option_ref)
        if container is None or option is None:
            raise DesktopPlanRefusal("unknown_control")
        if action.container_ref == action.option_ref:
            raise DesktopPlanRefusal("unknown_control")
        if not _is_reviewed_safe_select_container(container):
            raise DesktopPlanRefusal("unsupported_or_unknown_effect")


def validate_objective(value: object) -> str:
    if not isinstance(value, str):
        raise DesktopPlanRefusal("objective_invalid")
    text = value.strip()
    if not text or len(text) > MAX_OBJECTIVE_CHARS or _CONTROL.search(text):
        raise DesktopPlanRefusal("objective_invalid")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise DesktopPlanRefusal("objective_invalid") from None
    return text


def validate_model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise DesktopPlanRefusal("model_invalid")
    return value


def observation_age_seconds(observed_at: datetime, *, now: datetime | None = None) -> float:
    reference = now or datetime.now(UTC)
    return (reference - observed_at).total_seconds()
