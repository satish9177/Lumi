"""Cross-executor effect keys and the closed reconciliation registry (Milestone 10 S2, completed in S5).

M10 joins executors that were each bounded on their own. The composition risk is that an effect left
unresolved by one executor is simply *achieved again* by another: a download that may have happened is
"retried" by a second transfer, a placement that may have happened is "repeated" into the same folder, a
project run that may still be running is started again, a booking that may exist is submitted again from
a new task. The lock that stops this lives on the one action ledger, not in a separate database:

* every effect-bearing action writes its **effect keys** (`action_effect_keys`) in the transaction that
  creates it. Keys are derived here, by closed code, from the controller-built proposal -- never from a
  model, a page or the renderer;
* before any attempt of a keyed action is created, the ledger takes transaction-scoped advisory locks on
  its keys (sorted, so two claims cannot deadlock) and refuses if ANY other action -- in any task, from
  any executor -- holding one of those keys is `EXECUTING`, `OUTCOME_UNKNOWN` or `RECONCILING`;
* an unresolved effect of a **global-tier** kind (an external mutation, a project run) blocks every new
  keyed effect of any kind until it is reconciled. Stop and reconciliation are never blocked.

A model may extract candidate evidence; it never decides that an effect succeeded, that a retry is safe,
or that absence is authoritative. Those decisions are the registry's, per effect kind.
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class EffectKind(StrEnum):
    #: One network fetch of one approved URL into the quarantine.
    DOWNLOAD = "download"
    #: A new file appearing in an approved folder (placement).
    FILE_CREATE = "file_create"
    #: A supervised process tree started from a registered project recipe (S3).
    PROJECT_RUN = "project_run"
    #: A consequential change in another system (the booking fixture) (S5).
    EXTERNAL_MUTATION = "external_mutation"
    #: A bounded desktop mutation (M9 S4), keyed so it joins the same lock (S5).
    DESKTOP_MUTATION = "desktop_mutation"


#: An unresolved effect of one of these kinds blocks every new keyed effect of ANY kind.
GLOBAL_TIER: Final = frozenset({EffectKind.EXTERNAL_MUTATION, EffectKind.PROJECT_RUN})


@dataclass(frozen=True, slots=True)
class EffectKey:
    key: str
    kind: EffectKind


@dataclass(frozen=True, slots=True)
class ReconciliationRule:
    """What may establish the outcome of one effect kind. Closed; reviewed; never model-chosen."""

    kind: EffectKind
    correlation: tuple[str, ...]
    evidence_source: str
    read_only_operations: tuple[str, ...]
    #: Whether a lookup that finds nothing proves the effect did not happen.
    absence_is_authoritative: bool


RECONCILIATION_REGISTRY: Final[dict[EffectKind, ReconciliationRule]] = {
    EffectKind.DOWNLOAD: ReconciliationRule(
        kind=EffectKind.DOWNLOAD,
        correlation=("transfer_id",),
        evidence_source="the quarantine's started/complete markers and the payload hash",
        read_only_operations=("inspect_quarantine",),
        # Absence is authoritative only after reconciliation's own tombstone (it creates the transfer
        # directory, so the worker's exclusive `begin` -- which precedes any request -- can never succeed).
        absence_is_authoritative=True,
    ),
    EffectKind.FILE_CREATE: ReconciliationRule(
        kind=EffectKind.FILE_CREATE,
        correlation=("transfer_id", "quarantine_file_index"),
        evidence_source="the file index at the destination name and in the quarantine",
        read_only_operations=("inspect_destination", "inspect_quarantine"),
        # A same-volume rename is atomic: the file is in exactly one of the two places.
        absence_is_authoritative=True,
    ),
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def download_keys(*, source_url: str, root_id: str, file_name: str) -> tuple[EffectKey, ...]:
    return (
        EffectKey(key=f"transfer:source:{_digest(source_url)}", kind=EffectKind.DOWNLOAD),
        placement_key(root_id=root_id, file_name=file_name),
    )


def placement_key(*, root_id: str, file_name: str) -> EffectKey:
    # Windows names compare case-insensitively: two spellings of one name are one destination.
    return EffectKey(key=f"file:create:{root_id}:{_digest(file_name.casefold())}", kind=EffectKind.FILE_CREATE)


class EffectLockedError(Exception):
    """A new effect conflicts with an unresolved (or in-flight) one somewhere on the ledger."""

    def __init__(self, blocking_action_id: str, *, reason: str) -> None:
        super().__init__(f"An unresolved effect blocks this action ({reason}).")
        self.blocking_action_id = blocking_action_id
        self.reason = reason
