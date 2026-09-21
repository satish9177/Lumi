"""Milestone 9 S2: the disclosure service against real PostgreSQL.

Every test drives the real `DesktopDisclosureService`, repository and database. The only stand-in is
the S1 desktop *read* (`FakeDesktop`), which persists real observations exactly as the S1 service does;
"the provider" is a counter the test bumps after each successful claim, so "no automatic replay" is a
number that must stay at one.
"""

import asyncio
import json
import uuid
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.domain.desktop_disclosure import (
    MAX_OBSERVATION_AGE_SECONDS,
    STALE_CLAIM_SECONDS,
    DesktopDisclosureRefusal,
    build_projection,
)
from app.domain.errors import TaskNotAcceptingActionsError
from app.repositories.desktop_disclosure import DesktopDisclosureRepository
from app.services.desktop import DesktopService
from app.services.desktop_disclosure import DesktopDisclosureService, DesktopReadView, ProviderContext
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from tests.desktop_disclosure_support import (
    SECRET_DIGITS,
    SECRET_EMAIL,
    VISIBLE,
    WINDOW_A,
    WINDOW_B,
    FakeDesktop,
    node,
    observation,
    persist,
)

OBJECTIVE = "What is failing in this window?"
TITLE = "Editor - notes.txt"


@pytest.fixture
def desktop(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> FakeDesktop:
    return FakeDesktop(engine, runtime_generation.id)


@pytest.fixture
def service(engine: AsyncEngine, desktop: FakeDesktop) -> DesktopDisclosureService:
    return DesktopDisclosureService(engine, desktop=cast(DesktopService, desktop), grant_ttl_seconds=600)


class Provider:
    """The one provider the approval names. Only a successful claim may be followed by a call."""

    def __init__(self) -> None:
        self.calls = 0


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar()


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(sql), params)


async def open_card(service: DesktopDisclosureService, desktop: FakeDesktop) -> DesktopReadView:
    return await service.create(
        objective=OBJECTIVE,
        recipient="openai",
        model="gpt-test",
        worker_generation=desktop.worker_generation,
        surface_ref="s1",
        surface_epoch=1,
    )


async def approved(service: DesktopDisclosureService, desktop: FakeDesktop) -> DesktopReadView:
    view = await open_card(service, desktop)
    assert view.card is not None
    return await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)


def refusal_code(error: pytest.ExceptionInfo[DesktopDisclosureRefusal]) -> str:
    return error.value.code


GROUNDED = {
    "schema_version": 1,
    "kind": "answer",
    "answer": "3 failing tests.",
    "evidence": [{"control_ref": "u2", "quote": "3 failing tests"}],
}


# ---- create: local observation, then a card, and nothing sent ---------------------------------------


async def test_create_opens_a_pending_card_and_sends_nothing(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await open_card(service, desktop)
    assert view.phase == "awaiting_approval" and view.card is not None
    assert view.card.grant_status == "PENDING" and view.card.recipient == "openai" and view.card.model == "gpt-test"
    assert view.card.application_label == "Editor" and view.card.window_title == TITLE
    assert view.card.observation_available and view.card.node_count == 5
    assert view.disclosure is None and view.answer is None
    assert desktop.observe_calls == 1

    task = (await rows(engine, "SELECT request, status FROM tasks"))[0]
    assert task.request == {"type": "desktop_read", "objective": OBJECTIVE}
    assert task.status == "WAITING_APPROVAL"
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0
    grant = (await rows(engine, "SELECT kind, status, profile_id, expires_at FROM task_grants"))[0]
    assert (grant.kind, grant.status, grant.profile_id, grant.expires_at) == ("desktop_disclose", "PENDING", None, None)

    events = json.dumps([row.payload for row in await rows(engine, "SELECT payload FROM task_events")])
    for private in (TITLE, "Editor", VISIBLE, SECRET_EMAIL, OBJECTIVE, "failing"):
        assert private not in events


async def test_a_refused_observation_creates_no_task_and_no_grant(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    desktop.refusal = DesktopRefusal(DesktopReason.CREDENTIAL_SURFACE)
    with pytest.raises(DesktopRefusal) as refused:
        await open_card(service, desktop)
    assert refused.value.code is DesktopReason.CREDENTIAL_SURFACE
    for table in ("tasks", "task_grants", "task_events", "desktop_disclosures", "desktop_observations"):
        assert await scalar(engine, f"SELECT count(*) FROM {table}") == 0


async def test_a_stale_worker_generation_or_surface_is_refused_before_any_read(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    with pytest.raises(DesktopRefusal) as generation:
        await service.create(
            objective=OBJECTIVE, recipient="openai", model="gpt-test",
            worker_generation=uuid.uuid4(), surface_ref="s1", surface_epoch=1,
        )
    assert generation.value.code is DesktopReason.STALE_WORKER_GENERATION
    with pytest.raises(DesktopRefusal) as surface:
        await service.create(
            objective=OBJECTIVE, recipient="openai", model="gpt-test",
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=2,
        )
    assert surface.value.code is DesktopReason.STALE_SURFACE
    assert desktop.observe_calls == 0 and await scalar(engine, "SELECT count(*) FROM tasks") == 0


@pytest.mark.parametrize("objective", ["", "   ", "x" * 501, "line\x00break", 5])
async def test_an_invalid_typed_question_is_refused(
    service: DesktopDisclosureService, desktop: FakeDesktop, objective: Any
) -> None:
    with pytest.raises(DesktopDisclosureRefusal) as refused:
        await service.create(
            objective=objective, recipient="openai", model="gpt-test",
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
        )
    assert refused.value.code == "objective_invalid" and desktop.observe_calls == 0


# ---- the trusted click ---------------------------------------------------------------------------------


async def test_confirm_needs_the_revision_the_card_showed(
    service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await open_card(service, desktop)
    assert view.card is not None
    with pytest.raises(DesktopDisclosureRefusal) as wrong:
        await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision + 1)
    assert wrong.value.code == "grant_changed"
    with pytest.raises(DesktopDisclosureRefusal) as unknown:
        await service.confirm(view.task_id, grant_id=uuid.uuid4(), expected_revision=1)
    assert unknown.value.code == "grant_not_found"
    confirmed = await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)
    assert confirmed.phase == "approved" and confirmed.card is not None and confirmed.card.grant_status == "ACTIVE"
    with pytest.raises(DesktopDisclosureRefusal) as twice:
        await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)
    assert twice.value.code == "grant_not_pending"


async def test_a_declined_disclosure_cannot_be_claimed(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await open_card(service, desktop)
    assert view.card is not None
    declined = await service.revoke(
        view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision, reason="user_declined"
    )
    assert declined.phase == "declined" and declined.task_status == "CANCELLED"
    with pytest.raises(TaskNotAcceptingActionsError):
        await service.claim(view.task_id)
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0


async def test_an_unconfirmed_pending_grant_cannot_be_claimed(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await open_card(service, desktop)
    with pytest.raises(DesktopDisclosureRefusal) as refused:
        await service.claim(view.task_id)
    assert refused.value.code == "grant_not_active"
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0


async def test_a_snapshot_older_than_the_freshness_window_is_refused_at_confirm(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await open_card(service, desktop)
    assert view.card is not None
    await execute(engine, f"UPDATE desktop_observations SET created_at = now() - interval '{MAX_OBSERVATION_AGE_SECONDS + 60} seconds'")
    with pytest.raises(DesktopDisclosureRefusal) as stale:
        await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)
    assert stale.value.code == "observation_stale"


# ---- the claim: exact, single-use ---------------------------------------------------------------------------


async def test_claim_releases_the_projection_of_exactly_the_approved_observation(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    provider = Provider()
    context = await service.claim(view.task_id)
    provider.calls += 1

    assert context.objective == OBJECTIVE and context.recipient == "openai" and context.model == "gpt-test"
    dumped = json.dumps(context.projection, ensure_ascii=False)
    assert VISIBLE in dumped and "3 failing tests" in dumped
    assert SECRET_EMAIL not in dumped and SECRET_DIGITS not in dumped and TITLE not in dumped

    snapshot = (await rows(engine, "SELECT snapshot, snapshot_digest, created_at FROM desktop_observations"))[0]
    expected = build_projection(snapshot.snapshot, observed_at=context.observed_at)
    assert context.projection == expected.payload and context.projection_digest == expected.digest

    disclosure = (await rows(engine, "SELECT * FROM desktop_disclosures"))[0]
    assert disclosure.status == "STARTED" and disclosure.finished_at is None and disclosure.error_code is None
    assert disclosure.snapshot_digest == snapshot.snapshot_digest and disclosure.projection_digest == context.projection_digest
    grant = (await rows(engine, "SELECT status, completed_at FROM task_grants"))[0]
    assert grant.status == "COMPLETED" and grant.completed_at is not None
    assert (await rows(engine, "SELECT status FROM tasks"))[0].status == "EXECUTING"
    assert provider.calls == 1


async def test_a_second_claim_is_refused(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    provider = Provider()
    await service.claim(view.task_id)
    provider.calls += 1
    with pytest.raises(DesktopDisclosureRefusal) as second:
        await service.claim(view.task_id)
    assert second.value.code == "grant_not_active"
    assert provider.calls == 1 and await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 1


async def test_two_concurrent_claims_produce_exactly_one_disclosure(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    outcomes = await asyncio.gather(service.claim(view.task_id), service.claim(view.task_id), return_exceptions=True)
    contexts = [item for item in outcomes if isinstance(item, ProviderContext)]
    refusals = [item for item in outcomes if isinstance(item, DesktopDisclosureRefusal)]
    assert len(contexts) == 1 and len(refusals) == 1 and refusals[0].code == "grant_not_active"
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 1


async def test_the_database_itself_refuses_a_second_disclosure_for_one_grant_or_task(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    await service.claim(view.task_id)
    disclosure = (await rows(engine, "SELECT * FROM desktop_disclosures"))[0]
    values = {
        "task_id": disclosure.task_id, "observation_id": disclosure.observation_id,
        "snapshot_digest": disclosure.snapshot_digest, "recipient": "openai", "model": "gpt-test",
        "projection_digest": disclosure.projection_digest, "node_count": 1, "text_bytes": 1,
        "redaction_count": 0, "truncated": False,
    }
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await DesktopDisclosureRepository(connection).insert_disclosure(
                disclosure_id=uuid.uuid4(), grant_id=disclosure.grant_id, **values
            )
    # A different grant for the same task still cannot fund a second disclosure (UNIQUE task_id).
    grant = await _second_grant(engine, disclosure.task_id)
    async with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await DesktopDisclosureRepository(connection).insert_disclosure(
                disclosure_id=uuid.uuid4(), grant_id=grant, **values
            )
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 1


async def _second_grant(engine: AsyncEngine, task_id: uuid.UUID) -> uuid.UUID:
    from app.domain.desktop_disclosure import DesktopDiscloseScope, DisplayTarget

    row = (await rows(engine, "SELECT scope FROM task_grants"))[0]
    scope = DesktopDiscloseScope.model_validate({**row.scope, "display": DisplayTarget(application_label="a", window_title="b").model_dump()})
    async with engine.begin() as connection:
        grant = await DesktopDisclosureRepository(connection).insert_grant(grant_id=uuid.uuid4(), task_id=task_id, scope=scope)
    return grant.id


# ---- exactness: the wrong window, a changed window, a tampered or missing snapshot ---------------------------------


async def test_the_provider_sees_window_a_and_never_window_b(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    desktop.make_nodes = lambda: [node(1, name="Title", text=WINDOW_A)]
    view = await approved(service, desktop)
    # Window B is observed too (a different surface, same worker), and is more recent.
    other = observation(
        [node(1, name="Title", text=WINDOW_B)], worker_generation=desktop.worker_generation, surface_ref="s2"
    )
    await persist(engine, runtime_generation.id, other)
    context = await service.claim(view.task_id)
    dumped = json.dumps(context.projection)
    assert WINDOW_A in dumped and WINDOW_B not in dumped


async def test_a_later_observation_of_the_same_window_is_never_silently_substituted(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    desktop.make_nodes = lambda: [node(1, name="Doc", text="FIRST_DOCUMENT_O1")]
    view = await approved(service, desktop)
    desktop.make_nodes = lambda: [node(1, name="Doc", text="SECOND_DOCUMENT_O2 password hunter2")]
    second = await desktop.observe(desktop.worker_generation, "s1", 1)
    assert await scalar(engine, "SELECT count(*) FROM desktop_observations") == 2
    context = await service.claim(view.task_id)
    dumped = json.dumps(context.projection)
    assert "FIRST_DOCUMENT_O1" in dumped and "SECOND_DOCUMENT_O2" not in dumped
    disclosure = (await rows(engine, "SELECT observation_id FROM desktop_disclosures"))[0]
    assert disclosure.observation_id != second.observation_id


async def test_a_snapshot_whose_digest_no_longer_matches_the_approval_is_refused(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    await execute(engine, "UPDATE desktop_observations SET snapshot_digest = :digest", digest="b" * 64)
    with pytest.raises(DesktopDisclosureRefusal) as changed:
        await service.claim(view.task_id)
    assert changed.value.code == "observation_changed"
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0
    assert (await rows(engine, "SELECT status FROM task_grants"))[0].status == "ACTIVE"


async def test_a_pruned_observation_is_unavailable_and_nothing_is_sent(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    await execute(engine, "DELETE FROM desktop_observations")
    with pytest.raises(DesktopDisclosureRefusal) as gone:
        await service.claim(view.task_id)
    assert gone.value.code == "observation_unavailable"
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0


async def test_an_expired_grant_is_refused(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    await execute(
        engine,
        "UPDATE task_grants SET confirmed_at = now() - interval '10 seconds', expires_at = now() - interval '1 second'",
    )
    with pytest.raises(DesktopDisclosureRefusal) as expired:
        await service.claim(view.task_id)
    assert expired.value.code == "grant_expired"
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0
    assert (await service.describe(view.task_id)).phase == "expired"


# ---- recording the one attempt --------------------------------------------------------------------------------------


async def claimed(service: DesktopDisclosureService, desktop: FakeDesktop) -> ProviderContext:
    view = await approved(service, desktop)
    return await service.claim(view.task_id)


async def test_a_grounded_answer_is_recorded_privately(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    context = await claimed(service, desktop)
    view = await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=GROUNDED, failure=None)
    assert view.phase == "answered" and view.task_status == "SUCCEEDED"
    assert view.answer is not None and view.answer.answer == "3 failing tests."
    assert view.disclosure is not None and view.disclosure.status == "SUCCEEDED"
    answers = await rows(engine, "SELECT classification, kind, evidence, recipient FROM desktop_answers")
    assert len(answers) == 1 and answers[0].classification == "desktop_private" and answers[0].kind == "answer"
    assert answers[0].evidence == [{"control_ref": "u2", "quote": "3 failing tests"}]


async def test_a_cannot_answer_is_a_valid_grounded_outcome(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    context = await claimed(service, desktop)
    view = await service.record_result(
        context.task_id, disclosure_id=context.disclosure_id,
        result={"schema_version": 1, "kind": "cannot_answer", "reason": "not_in_snapshot"}, failure=None,
    )
    assert view.phase == "answered" and view.answer is not None and view.answer.kind == "cannot_answer"
    assert view.answer.reason == "not_in_snapshot" and view.answer.evidence == []


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({**GROUNDED, "evidence": [{"control_ref": "u2", "quote": "12 failing tests"}], "answer": "12 failing tests."}, "answer_not_grounded"),
        ({**GROUNDED, "evidence": [{"control_ref": "u77", "quote": "3 failing tests"}]}, "answer_not_grounded"),
        ({**GROUNDED, "answer": "9 failing tests."}, "answer_not_grounded"),
        ({**GROUNDED, "evidence": [{"control_ref": "u4", "quote": SECRET_EMAIL}], "answer": "A contact."}, "answer_not_grounded"),
        ({**GROUNDED, "operation": "invoke"}, "invalid_output"),
        ({**GROUNDED, "tool": "desktop", "action": "click", "coordinates": [1, 2]}, "invalid_output"),
        ({"kind": "answer"}, "invalid_output"),
        ({"schema_version": 1, "kind": "cannot_answer", "reason": "I decided so"}, "invalid_output"),
    ],
)
async def test_an_ungrounded_or_malformed_result_ends_the_attempt_and_stores_no_answer(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop, result: dict[str, Any], code: str
) -> None:
    context = await claimed(service, desktop)
    view = await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=result, failure=None)
    assert view.phase == "failed" and view.task_status == "FAILED" and view.answer is None
    assert view.disclosure is not None and view.disclosure.status == "FAILED" and view.disclosure.error_code == code
    assert await scalar(engine, "SELECT count(*) FROM desktop_answers") == 0
    # The approval is spent and the task is terminal: nothing can claim again.
    with pytest.raises(TaskNotAcceptingActionsError):
        await service.claim(context.task_id)


@pytest.mark.parametrize("failure", ["model_unavailable", "invalid_output"])
async def test_a_provider_failure_is_recorded_as_final(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop, failure: str
) -> None:
    context = await claimed(service, desktop)
    view = await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=None, failure=failure)
    assert view.phase == "failed" and view.disclosure is not None and view.disclosure.error_code == failure
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures WHERE status = 'FAILED'") == 1


async def test_a_result_must_be_exactly_one_of_answer_or_failure_and_failures_are_closed(
    service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    context = await claimed(service, desktop)
    with pytest.raises(DesktopDisclosureRefusal) as neither:
        await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=None, failure=None)
    assert neither.value.code == "result_malformed"
    with pytest.raises(DesktopDisclosureRefusal) as both:
        await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=GROUNDED, failure="model_unavailable")
    assert both.value.code == "result_malformed"
    with pytest.raises(DesktopDisclosureRefusal) as open_code:
        await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=None, failure="answer_not_grounded")
    assert open_code.value.code == "failure_invalid"
    with pytest.raises(DesktopDisclosureRefusal) as unknown:
        await service.record_result(context.task_id, disclosure_id=uuid.uuid4(), result=GROUNDED, failure=None)
    assert unknown.value.code == "disclosure_not_started"


async def test_a_result_can_only_be_recorded_once(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    context = await claimed(service, desktop)
    await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=GROUNDED, failure=None)
    with pytest.raises(DesktopDisclosureRefusal) as again:
        await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=GROUNDED, failure=None)
    assert again.value.code == "disclosure_already_recorded"
    assert await scalar(engine, "SELECT count(*) FROM desktop_answers") == 1


# ---- lost responses: OUTCOME_UNKNOWN, never a replay ------------------------------------------------------------------


async def test_recovery_before_a_claim_changes_nothing_and_the_grant_is_still_claimable(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    assert await service.recover_started() == 0
    assert (await rows(engine, "SELECT status FROM task_grants"))[0].status == "ACTIVE"
    provider = Provider()
    await service.claim(view.task_id)
    provider.calls += 1
    assert provider.calls == 1


async def test_a_claim_lost_before_its_result_becomes_outcome_unknown_and_is_never_replayed(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    provider = Provider()
    context = await service.claim(view.task_id)
    provider.calls += 1  # the one provider attempt, whose response the "dead runtime" never recorded

    assert await service.recover_started() == 1
    disclosure = (await rows(engine, "SELECT status, error_code, finished_at FROM desktop_disclosures"))[0]
    assert disclosure.status == "OUTCOME_UNKNOWN" and disclosure.error_code == "runtime_restart" and disclosure.finished_at
    described = await service.describe(view.task_id)
    assert described.phase == "outcome_unknown" and described.task_status == "OUTCOME_UNKNOWN"

    with pytest.raises(DesktopDisclosureRefusal) as replay:
        await service.claim(view.task_id)
    assert replay.value.code == "grant_not_active"
    with pytest.raises(DesktopDisclosureRefusal) as late:
        await service.record_result(context.task_id, disclosure_id=context.disclosure_id, result=GROUNDED, failure=None)
    assert late.value.code == "disclosure_already_recorded"
    assert await service.recover_started() == 0
    assert provider.calls == 1
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 1
    assert await scalar(engine, "SELECT count(*) FROM desktop_answers") == 0


async def test_a_claim_that_never_reported_is_not_left_in_flight_forever(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await approved(service, desktop)
    await service.claim(view.task_id)
    assert (await service.describe(view.task_id)).phase == "reasoning"
    await execute(engine, f"UPDATE desktop_disclosures SET started_at = now() - interval '{STALE_CLAIM_SECONDS + 60} seconds'")
    described = await service.describe(view.task_id)
    assert described.phase == "outcome_unknown" and described.task_status == "OUTCOME_UNKNOWN"


# ---- cancelling ----------------------------------------------------------------------------------------------------------


async def test_cancelling_a_task_revokes_an_unclaimed_disclosure(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    view = await open_card(service, desktop)
    await TaskService(engine).cancel_task(view.task_id)
    assert (await rows(engine, "SELECT status FROM task_grants"))[0].status == "REVOKED"
    with pytest.raises(TaskNotAcceptingActionsError):
        await service.claim(view.task_id)
    assert await scalar(engine, "SELECT count(*) FROM desktop_disclosures") == 0


async def test_cancelling_after_a_claim_cannot_unsend_it(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    context = await claimed(service, desktop)
    await TaskService(engine).cancel_task(context.task_id)
    assert (await rows(engine, "SELECT status FROM task_grants"))[0].status == "COMPLETED"
    assert (await rows(engine, "SELECT status FROM desktop_disclosures"))[0].status == "STARTED"


# ---- nothing raw leaves the observation ----------------------------------------------------------------------------------


async def test_the_raw_sensitive_values_exist_only_in_the_local_observation(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    context = await claimed(service, desktop)
    await service.record_result(
        context.task_id, disclosure_id=context.disclosure_id,
        result={**GROUNDED, "answer": "A contact email exists.", "evidence": [{"control_ref": "u4", "quote": "⟦email:1⟧"}]},
        failure=None,
    )
    for raw in (SECRET_EMAIL, SECRET_DIGITS):
        assert raw in json.dumps((await rows(engine, "SELECT snapshot FROM desktop_observations"))[0].snapshot)
        for table in ("task_events", "desktop_disclosures", "desktop_answers", "task_grants", "tasks", "task_grants"):
            dump = json.dumps([list(map(str, row)) for row in await rows(engine, f"SELECT * FROM {table}")])
            assert raw not in dump, f"{raw} leaked into {table}"
    # The window title is display data on the grant card and appears in no event.
    events = json.dumps([row.payload for row in await rows(engine, "SELECT payload FROM task_events")])
    assert TITLE not in events and OBJECTIVE not in events
