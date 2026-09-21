"""Which profiles are in form preparation, and which hold a local draft (Milestone 8b S6).

In memory, on purpose. A preparation window and a dirty page are *browser* state: they
die with the worker, the runtime and the computer, so the truth about them cannot be
stored and restored -- it can only be re-derived. Every restart therefore starts with an
empty registry, and recovery closes any `form_drafts` row that says otherwise.

```text
PREPARATION   the profile was reopened headed for form preparation; nothing is written
FILLING       an approved local draft is being written under the freeze
DIRTY         the page holds a local draft (PREPARED or STALE); the network is frozen
HANDOVER      the second approval is being carried out; the network may already be back
UNKNOWN       a handover response was lost: whether the network returned is not known
```

While a profile is anywhere but `PREPARATION`, no read step, no planning, no takeover,
no close and no account change may touch it (`form_is_dirty`). It is the runtime half of
a rule the worker enforces independently.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.form_prepare import FormPrepareRefusal


class FormPhase(StrEnum):
    PREPARATION = "PREPARATION"
    FILLING = "FILLING"
    DIRTY = "DIRTY"
    HANDOVER = "HANDOVER"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ProfileFormState:
    task_id: uuid.UUID
    worker_generation: uuid.UUID
    phase: FormPhase = FormPhase.PREPARATION
    #: Page observations recorded while the profile was in preparation mode. A manifest
    #: is only executable if it was built from one of these.
    observation_ids: set[uuid.UUID] = field(default_factory=set)
    action_id: uuid.UUID | None = None
    #: The dispatch that owns the worker's freeze (the fill dispatch).
    dispatch_id: uuid.UUID | None = None
    draft_id: uuid.UUID | None = None


class FormStateRegistry:
    def __init__(self) -> None:
        self._states: dict[uuid.UUID, ProfileFormState] = {}

    def get(self, profile_id: uuid.UUID) -> ProfileFormState | None:
        return self._states.get(profile_id)

    def for_task(self, task_id: uuid.UUID) -> tuple[uuid.UUID, ProfileFormState] | None:
        for profile_id, state in self._states.items():
            if state.task_id == task_id:
                return profile_id, state
        return None

    def begin_preparation(
        self, profile_id: uuid.UUID, *, task_id: uuid.UUID, worker_generation: uuid.UUID
    ) -> ProfileFormState:
        existing = self._states.get(profile_id)
        if existing is not None and existing.phase is not FormPhase.PREPARATION:
            raise FormPrepareRefusal("form_is_dirty")
        state = ProfileFormState(task_id=task_id, worker_generation=worker_generation)
        self._states[profile_id] = state
        return state

    def is_preparing(self, profile_id: uuid.UUID) -> bool:
        state = self._states.get(profile_id)
        return state is not None and state.phase is FormPhase.PREPARATION

    def note_observation(self, profile_id: uuid.UUID, observation_id: uuid.UUID) -> None:
        state = self._states.get(profile_id)
        if state is not None and state.phase is FormPhase.PREPARATION:
            state.observation_ids.add(observation_id)

    def assert_clean(self, profile_id: uuid.UUID) -> None:
        """Refuse (`form_is_dirty`) unless nothing has been, or is being, written."""
        state = self._states.get(profile_id)
        if state is not None and state.phase is not FormPhase.PREPARATION:
            raise FormPrepareRefusal("form_is_dirty")

    def is_dirty(self, profile_id: uuid.UUID) -> bool:
        state = self._states.get(profile_id)
        return state is not None and state.phase is not FormPhase.PREPARATION

    def clear(self, profile_id: uuid.UUID) -> None:
        self._states.pop(profile_id, None)


__all__ = ["FormPhase", "FormStateRegistry", "ProfileFormState"]
