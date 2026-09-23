"""Cross-executor effect keys and the closed effect registry (Milestone 10 S2, completed in S5).

M10 joins executors that were each bounded on their own. The composition risk is that an effect left
unresolved by one executor is simply *achieved again* by another: a download that may have happened is
"retried" by a second transfer, a placement that may have happened is "repeated" into the same folder, a
project run that may still be running is started again, a booking that may exist is submitted again from
a new task. The lock that stops this lives on the one action ledger, not in a separate database:

* every effect-bearing tool is named in `EFFECT_TOOLS`. Its keys are derived here, by closed code, from
  the persisted, controller-built proposal (or, for a project start, supplied by the controller from the
  confirmed grant and checked against the tool's registered kind) -- never from a model, a page, a
  provider or the renderer;
* `ActionService` is the single choke point: every path that moves an action to `EXECUTING`
  (`start_attempt`, `begin_exact_execution`, `start_scoped_attempt`) resolves the keys through
  `resolve_effect_keys`, writes them in that transaction if they are not already there, takes
  transaction-scoped advisory locks (the global lock first, then the keys, sorted, so two claims cannot
  deadlock) and refuses if ANY other action -- in any task, from any executor -- holding one of those keys
  is `EXECUTING`, `OUTCOME_UNKNOWN` or `RECONCILING`;
* an unresolved or in-flight effect of a **global-tier** kind (an external mutation, a project run)
  blocks every new keyed effect of any kind until it is reconciled. Stop and reconciliation are never
  blocked.

A model may extract candidate evidence; it never decides that an effect succeeded, that a retry is safe,
or that absence is authoritative. Those decisions are the registry's, per effect kind.
"""

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


class EffectKind(StrEnum):
    #: One network fetch of one approved URL into the quarantine.
    DOWNLOAD = "download"
    #: A new file appearing in an approved folder (placement).
    FILE_CREATE = "file_create"
    #: A supervised process tree started from a registered project recipe (S3).
    PROJECT_RUN = "project_run"
    #: A consequential change in another system (the booking fixture) (S5).
    EXTERNAL_MUTATION = "external_mutation"
    #: A bounded desktop mutation (M9 S4 set value / select / invoke), keyed so it joins the same lock (S5).
    DESKTOP_MUTATION = "desktop_mutation"


#: An unresolved (or in-flight) effect of one of these kinds blocks every new keyed effect of ANY kind.
GLOBAL_TIER: Final = frozenset({EffectKind.EXTERNAL_MUTATION, EffectKind.PROJECT_RUN})


@dataclass(frozen=True, slots=True)
class EffectKey:
    key: str
    kind: EffectKind


class AbsenceAuthority(StrEnum):
    """Whether a read-only look that finds nothing proves the effect did not happen."""

    #: Always, by construction of the evidence (a tombstone, an atomic rename).
    AUTHORITATIVE = "authoritative"
    #: Only where the reviewed site declaration (`app.domain.sites`) says so. Unknown sites: never.
    SITE_DECLARED = "site_declared"
    #: Never. Not finding the effect is evidence about the look, not about the world.
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class ReconciliationRule:
    """What may establish the outcome of one effect kind. Closed; reviewed; never model-chosen."""

    kind: EffectKind
    correlation: tuple[str, ...]
    evidence_source: str
    read_only_operations: tuple[str, ...]
    absence: AbsenceAuthority
    #: Upper bound on read-only lookups for one action, and the backoff between them (0 = none).
    max_lookups: int | None = None
    lookup_backoff_seconds: tuple[int, ...] = ()
    #: Settled only by the person saying what they saw (never by Lumi inferring it).
    human_attestation: bool = False

    @property
    def absence_is_authoritative(self) -> bool:
        return self.absence is AbsenceAuthority.AUTHORITATIVE


RECONCILIATION_REGISTRY: Final[dict[EffectKind, ReconciliationRule]] = {
    EffectKind.DOWNLOAD: ReconciliationRule(
        kind=EffectKind.DOWNLOAD,
        correlation=("transfer_id",),
        evidence_source="the quarantine's started/complete markers and the payload hash",
        read_only_operations=("inspect_quarantine",),
        # Absence is authoritative only after reconciliation's own tombstone (it creates the transfer
        # directory, so the worker's exclusive `begin` -- which precedes any request -- can never succeed).
        absence=AbsenceAuthority.AUTHORITATIVE,
    ),
    EffectKind.FILE_CREATE: ReconciliationRule(
        kind=EffectKind.FILE_CREATE,
        correlation=("transfer_id", "quarantine_file_index"),
        evidence_source="the file index at the destination name and in the quarantine",
        read_only_operations=("inspect_destination", "inspect_quarantine"),
        # A same-volume rename is atomic: the file is in exactly one of the two places.
        absence=AbsenceAuthority.AUTHORITATIVE,
    ),
    EffectKind.PROJECT_RUN: ReconciliationRule(
        kind=EffectKind.PROJECT_RUN,
        correlation=("run_id", "pid", "creation_time"),
        evidence_source="the run row, the recorded (pid, creation time) and the run's own job",
        read_only_operations=("process_is_alive",),
        # Only "no (pid, creation time) was ever recorded" proves no project code ran (the process was never
        # resumed). A process that is gone proves only that the run is over, never that it had no effect.
        absence=AbsenceAuthority.NEVER,
    ),
    EffectKind.EXTERNAL_MUTATION: ReconciliationRule(
        kind=EffectKind.EXTERNAL_MUTATION,
        correlation=("action_id", "booking_reference"),
        evidence_source="the site's own booking lookup, by the reference derived from the action id",
        read_only_operations=("lookup_booking",),
        absence=AbsenceAuthority.SITE_DECLARED,
        # User-triggered only (there is no automatic loop). Two immediate looks, then a growing wait; at most
        # `max_lookups` per action in any rolling day. Hitting the bound never changes the action's status.
        max_lookups=8,
        lookup_backoff_seconds=(0, 0, 15, 30, 60, 120, 300, 600),
    ),
    EffectKind.DESKTOP_MUTATION: ReconciliationRule(
        kind=EffectKind.DESKTOP_MUTATION,
        correlation=("action_id", "dispatch_id"),
        evidence_source="the person's own look at the application (M9 S4 `reconcile`)",
        read_only_operations=(),
        # Lumi never infers absence here. The only way out is the person's explicit attestation of what they
        # saw ("succeeded" / "failed" / "still unknown"), a separately reviewed M9 path -- not a lookup.
        absence=AbsenceAuthority.NEVER,
        human_attestation=True,
    ),
}

#: The rolling window `max_lookups` counts over.
LOOKUP_WINDOW_SECONDS: Final = 24 * 60 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_key(source_url: str) -> EffectKey:
    return EffectKey(key=f"transfer:source:{_digest(source_url)}", kind=EffectKind.DOWNLOAD)


def download_keys(*, source_url: str, root_id: str, file_name: str) -> tuple[EffectKey, ...]:
    return (source_key(source_url), placement_key(root_id=root_id, file_name=file_name))


def fold_file_name(file_name: str) -> str:
    """Windows names compare case-insensitively through NTFS's upcase table, which is not Unicode case folding
    (a dotless "ı" upcases to "I"). Upper-casing first and then folding joins every pair NTFS joins, and may join
    a few it does not (only ever over-locking). ASCII names fold exactly as `casefold` alone did."""
    return file_name.upper().casefold()


def placement_key(*, root_id: str, file_name: str) -> EffectKey:
    # Two spellings of one Windows name are one destination.
    return EffectKey(key=f"file:create:{root_id.lower()}:{_digest(fold_file_name(file_name))}", kind=EffectKind.FILE_CREATE)


_SITE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def booking_key(site: object) -> EffectKey:
    # One key per reviewed site. A proposal without a well-formed site still gets a (conservative) key: it is
    # never unkeyed, and the booking executor refuses to parse it long before any browser is involved.
    name = site if isinstance(site, str) and _SITE.match(site) else "unparsed"
    return EffectKey(key=f"booking:site:{name}", kind=EffectKind.EXTERNAL_MUTATION)


#: M9 already lets at most one desktop action run, and an unresolved mutation blocks every desktop action;
#: one shared key makes the ledger enforce the same rule for the mutations, across tasks and executors.
DESKTOP_MUTATION_KEY: Final = EffectKey(key="desktop:mutation:all", kind=EffectKind.DESKTOP_MUTATION)


# ---- the closed tool registry --------------------------------------------------------------------------------

#: Tool names, spelled here so this module imports nothing (pinned against their owners by a test).
TOOL_COMMIT_BOOKING: Final = "commit_booking"
TOOL_TRANSFER_DOWNLOAD: Final = "transfer_download"
TOOL_TRANSFER_PLACE: Final = "transfer_place"
TOOL_PROJECT_START: Final = "project_start"
TOOL_DESKTOP_SET_VALUE: Final = "DESKTOP_SET_VALUE"
TOOL_DESKTOP_SELECT: Final = "DESKTOP_SELECT"
TOOL_DESKTOP_INVOKE: Final = "DESKTOP_INVOKE"

Derive = Callable[[Mapping[str, Any]], tuple[EffectKey, ...]]


@dataclass(frozen=True, slots=True)
class EffectTool:
    """One effect-bearing tool. `derive` is None only where the proposal does not carry the key's identity
    (a project start names the run, not the project); the owning controller then supplies the keys from the
    confirmed grant, and they must be exactly one key per registered kind."""

    tool_name: str
    kinds: frozenset[EffectKind]
    derive: Derive | None
    #: The only route family allowed to move this tool's actions (named in refusals and docs).
    controller: str


def _download(proposal: Mapping[str, Any]) -> tuple[EffectKey, ...]:
    return download_keys(
        source_url=str(proposal["source_url"]), root_id=str(proposal["dest_root_id"]), file_name=str(proposal["dest_name"])
    )


def _place(proposal: Mapping[str, Any]) -> tuple[EffectKey, ...]:
    return (placement_key(root_id=str(proposal["dest_root_id"]), file_name=str(proposal["dest_name"])),)


def _booking(proposal: Mapping[str, Any]) -> tuple[EffectKey, ...]:
    return (booking_key(proposal.get("site")),)


def _desktop(_proposal: Mapping[str, Any]) -> tuple[EffectKey, ...]:
    return (DESKTOP_MUTATION_KEY,)


EFFECT_TOOLS: Final[dict[str, EffectTool]] = {
    tool.tool_name: tool
    for tool in (
        EffectTool(TOOL_COMMIT_BOOKING, frozenset({EffectKind.EXTERNAL_MUTATION}), _booking, "booking"),
        EffectTool(TOOL_TRANSFER_DOWNLOAD, frozenset({EffectKind.DOWNLOAD, EffectKind.FILE_CREATE}), _download, "transfer"),
        EffectTool(TOOL_TRANSFER_PLACE, frozenset({EffectKind.FILE_CREATE}), _place, "transfer"),
        EffectTool(TOOL_PROJECT_START, frozenset({EffectKind.PROJECT_RUN}), None, "project"),
        EffectTool(TOOL_DESKTOP_SET_VALUE, frozenset({EffectKind.DESKTOP_MUTATION}), _desktop, "desktop"),
        EffectTool(TOOL_DESKTOP_SELECT, frozenset({EffectKind.DESKTOP_MUTATION}), _desktop, "desktop"),
        EffectTool(TOOL_DESKTOP_INVOKE, frozenset({EffectKind.DESKTOP_MUTATION}), _desktop, "desktop"),
    )
}


def effect_tool(tool_name: str) -> EffectTool | None:
    """Exact, case-sensitive: the ledger's tool names are exact, and a near-miss spelling is simply not an
    effect tool (every executor selects by the same exact name, so it can never run as one either)."""
    return EFFECT_TOOLS.get(tool_name)


def is_effect_tool_name(tool_name: str) -> bool:
    """For refusing generic callers: ANY spelling that folds onto a registered tool counts."""
    folded = tool_name.casefold()
    return any(folded == name.casefold() for name in EFFECT_TOOLS)


def unparsed_keys(tool_name: str) -> tuple[EffectKey, ...]:
    """Conservative keys for an unresolved action whose keys cannot be derived (startup backfill only): one
    per registered kind, so it still blocks what its kind blocks -- and, holding keys the registry does not
    derive, it can never start another attempt either."""
    tool = EFFECT_TOOLS[tool_name]
    return tuple(EffectKey(key=f"unparsed:{kind.value}:{tool_name.lower()}", kind=kind) for kind in sorted(tool.kinds))


class EffectKeysError(Exception):
    """A registered effect tool without its keys, or with keys it does not own. A programming error, and
    fail-closed: nothing is written."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"effect keys for {tool_name!r}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


def resolve_effect_keys(
    tool_name: str, proposal: Mapping[str, Any], supplied: tuple[EffectKey, ...] = ()
) -> tuple[EffectKey, ...]:
    """The keys an action of this tool holds. Empty only for a tool that is not effect-bearing."""
    tool = effect_tool(tool_name)
    if tool is None:
        if supplied:
            raise EffectKeysError(tool_name, "keys were supplied for a tool that is not registered")
        return ()
    if tool.derive is not None:
        try:
            derived = tool.derive(proposal)
        except (KeyError, TypeError) as error:
            raise EffectKeysError(tool_name, "the proposal does not carry the key's identity") from error
        if supplied and sorted(supplied, key=lambda item: item.key) != sorted(derived, key=lambda item: item.key):
            raise EffectKeysError(tool_name, "the supplied keys differ from the registry's")
        return derived
    if not supplied:
        raise EffectKeysError(tool_name, "the controller must supply this tool's keys")
    if sorted(key.kind for key in supplied) != sorted(tool.kinds):
        raise EffectKeysError(tool_name, "the supplied keys are not exactly one per registered kind")
    return supplied


class EffectRouteRefusal(Exception):
    """A generic ledger route asked to create, start, finish or settle a registered effect tool. Only the
    tool's own controller (`EffectTool.controller`) may; the generic routes can only ever hold inert actions."""

    def __init__(self, tool_name: str) -> None:
        tool = next((item for name, item in EFFECT_TOOLS.items() if name.casefold() == tool_name.casefold()), None)
        self.code = f"use_{tool.controller}_route" if tool is not None else "use_controller_route"
        super().__init__(f"{tool_name!r} is an effect tool: {self.code}")


def lookup_allowance(rule: ReconciliationRule, *, previous_ages_seconds: list[float]) -> tuple[bool, str, int]:
    """Whether one more read-only lookup may run now, from the ages of this action's earlier lookups.

    Returns `(allowed, reason, retry_after_seconds)`. Only lookups inside the rolling window count toward
    `max_lookups`; the backoff is measured from the newest one. A refusal is a refusal to *look* -- it never
    changes what is known about the effect.
    """
    if rule.max_lookups is None:
        return True, "", 0
    # A negative age (clock skew, or a look that committed after this transaction began) counts as "just now".
    recent = sorted(max(0.0, age) for age in previous_ages_seconds if age < LOOKUP_WINDOW_SECONDS)
    if len(recent) >= rule.max_lookups:
        return False, "lookup_budget_exhausted", int(LOOKUP_WINDOW_SECONDS - recent[-1]) + 1
    if recent and rule.lookup_backoff_seconds:
        wait = rule.lookup_backoff_seconds[min(len(recent), len(rule.lookup_backoff_seconds) - 1)]
        if recent[0] < wait:
            return False, "lookup_backoff", int(wait - recent[0]) + 1
    return True, "", 0


class ReconciliationLimitedError(Exception):
    """A read-only reconciliation lookup refused by its bound. The action's status is untouched."""

    def __init__(self, action_id: str, *, reason: str, retry_after_seconds: int) -> None:
        super().__init__(f"reconciliation of {action_id} is limited ({reason}); retry after {retry_after_seconds}s")
        self.action_id = action_id
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class EffectLockedError(Exception):
    """A new effect conflicts with an unresolved (or in-flight) one somewhere on the ledger."""

    def __init__(self, blocking_action_id: str, *, reason: str) -> None:
        super().__init__(f"An unresolved effect blocks this action ({reason}).")
        self.blocking_action_id = blocking_action_id
        self.reason = reason
