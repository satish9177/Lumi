"""The action lifecycle over HTTP, including what the API refuses."""

from typing import Any

import httpx
import pytest

from app.domain.digest import proposal_digest

REQUEST = {"type": "appointment_booking", "text": "Book Saturday evening"}
PROPOSAL = {
    "appointment_id": "slot-123",
    "doctor": "Dr Example",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
}
PROPOSE = {
    "idempotency_key": "booking-001",
    "tool_name": "commit_booking",
    "risk_tier": "R2",
    "proposal": PROPOSAL,
}


def _body(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return body


async def _task(client: httpx.AsyncClient) -> str:
    response = await client.post("/tasks", json={"request": REQUEST})
    assert response.status_code == 201
    return str(response.json()["id"])


async def _propose(
    client: httpx.AsyncClient, task_id: str, **overrides: Any
) -> httpx.Response:
    return await client.post(f"/tasks/{task_id}/actions", json={**PROPOSE, **overrides})


async def _approved_action(client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
    action = (await _propose(client, task_id)).json()
    action = (await client.post(f"/actions/{action['id']}/approval-request")).json()
    response = await client.post(
        f"/actions/{action['id']}/approve", json={"expected_revision": action["revision"]}
    )
    assert response.status_code == 200
    return _body(response)


async def _executing_action(client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
    action = await _approved_action(client, task_id)
    response = await client.post(
        f"/actions/{action['id']}/attempts", json={"expected_revision": action["revision"]}
    )
    assert response.status_code == 201
    return _body(response)


async def _event_types(client: httpx.AsyncClient, task_id: str) -> list[str]:
    events = (await client.get(f"/tasks/{task_id}/events")).json()["events"]
    return [event["event_type"] for event in events]


# ---- proposal ---------------------------------------------------------------


async def test_proposing_an_action_stores_it_and_records_one_event(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)

    response = await _propose(client, task_id)

    assert response.status_code == 201
    action = response.json()
    assert action["status"] == "PROPOSED"
    assert action["revision"] == 1
    assert action["proposal"] == PROPOSAL
    assert action["risk_tier"] == "R2"
    assert action["approval"] is None
    assert action["attempts"] == []
    assert await _event_types(client, task_id) == ["task.created", "action.proposed"]


async def test_the_server_computes_the_digest_itself(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = (await _propose(client, task_id)).json()
    assert action["proposal_digest"] == proposal_digest(PROPOSAL)


async def test_a_caller_supplied_digest_is_rejected_outright(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    # Accepting one would let a caller bind an approval to bytes never stored.
    response = await _propose(client, task_id, proposal_digest="0" * 64)
    assert response.status_code == 422


async def test_the_task_timeline_never_carries_the_proposal_body(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    await _propose(client, task_id)

    events = (await client.get(f"/tasks/{task_id}/events")).json()["events"]

    payload = events[-1]["payload"]
    assert payload["proposal_digest"] == proposal_digest(PROPOSAL)
    assert "proposal" not in payload
    assert "Dr Example" not in str(events)


async def test_proposing_on_a_cancelled_task_is_refused(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    await client.post(f"/tasks/{task_id}/cancel")

    response = await _propose(client, task_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_not_accepting_actions"


async def test_proposing_on_an_unknown_task_is_not_found(client: httpx.AsyncClient) -> None:
    response = await _propose(client, "00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404


async def test_actions_are_listed_for_their_task(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    first = (await _propose(client, task_id)).json()
    second = (await _propose(client, task_id, idempotency_key="booking-002")).json()

    listed = (await client.get(f"/tasks/{task_id}/actions")).json()

    assert [action["id"] for action in listed["actions"]] == [first["id"], second["id"]]


async def test_an_unknown_action_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.get("/actions/00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "action_not_found"


# ---- idempotency ------------------------------------------------------------


async def test_the_same_key_and_proposal_returns_the_stored_action(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    first = (await _propose(client, task_id)).json()

    # Same proposal, keys written in a different order.
    replay = await _propose(
        client,
        task_id,
        proposal={
            "price": 800,
            "time": "2026-09-19T18:30:00+05:30",
            "doctor": "Dr Example",
            "appointment_id": "slot-123",
        },
    )

    assert replay.status_code == 200  # 200, not 201: nothing was created.
    assert replay.json() == first
    assert await _event_types(client, task_id) == ["task.created", "action.proposed"]


async def test_the_same_key_with_a_different_proposal_is_a_conflict(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    first = (await _propose(client, task_id)).json()

    response = await _propose(client, task_id, proposal={**PROPOSAL, "price": 9_000})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "action_proposal_conflict"
    # The stored proposal is untouched; an approval may already be bound to it.
    assert (await client.get(f"/actions/{first['id']}")).json()["proposal"] == PROPOSAL
    assert await _event_types(client, task_id) == ["task.created", "action.proposed"]


async def test_the_same_key_with_a_different_tool_is_a_conflict(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    await _propose(client, task_id)

    response = await _propose(client, task_id, tool_name="cancel_booking")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "action_proposal_conflict"


async def test_the_same_key_on_another_task_is_a_separate_action(
    client: httpx.AsyncClient,
) -> None:
    first_task, second_task = await _task(client), await _task(client)

    first = (await _propose(client, first_task)).json()
    second = (await _propose(client, second_task)).json()

    assert first["id"] != second["id"]


# ---- approval ---------------------------------------------------------------


async def test_requesting_approval_opens_an_expiring_pending_approval(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = (await _propose(client, task_id)).json()

    response = await client.post(f"/actions/{action['id']}/approval-request")

    assert response.status_code == 200
    updated = response.json()
    assert updated["status"] == "WAITING_APPROVAL"
    assert updated["approval"]["status"] == "PENDING"
    assert updated["approval"]["proposal_digest"] == action["proposal_digest"]
    assert updated["approval"]["expires_at"] > updated["approval"]["created_at"]
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "WAITING_APPROVAL"


async def test_approving_grants_the_approval_and_binds_it_to_the_new_revision(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)

    action = await _approved_action(client, task_id)

    assert action["status"] == "APPROVED"
    assert action["approval"]["status"] == "APPROVED"
    assert action["approval"]["approved_at"] is not None
    # Bound to the revision approval produced, so any later change unbinds it.
    assert action["approval"]["action_revision"] == action["revision"]
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "READY"
    assert await _event_types(client, task_id) == [
        "task.created",
        "action.proposed",
        "action.approval_requested",
        "action.approved",
    ]


async def test_approving_without_a_request_is_refused(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = (await _propose(client, task_id)).json()

    response = await client.post(f"/actions/{action['id']}/approve")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_action_transition"


async def test_approving_twice_is_refused(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = await _approved_action(client, task_id)

    response = await client.post(f"/actions/{action['id']}/approve")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_action_transition"


async def test_a_stale_expected_revision_cannot_approve(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = (await _propose(client, task_id)).json()
    action = (await client.post(f"/actions/{action['id']}/approval-request")).json()

    response = await client.post(
        f"/actions/{action['id']}/approve", json={"expected_revision": 1}
    )

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "stale_action_revision"
    assert body["current_revision"] == action["revision"]
    assert (await client.get(f"/actions/{action['id']}")).json()["status"] == "WAITING_APPROVAL"


async def test_rejecting_closes_the_approval_and_blocks_execution(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _approved_action(client, task_id)

    rejected = await client.post(
        f"/actions/{action['id']}/reject",
        json={"expected_revision": action["revision"], "reason": "wrong doctor"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["approval"] is None  # No live approval remains.
    started = await client.post(f"/actions/{action['id']}/attempts")
    assert started.status_code == 409
    assert started.json()["error"]["code"] == "invalid_action_transition"
    assert "action.rejected" in await _event_types(client, task_id)


async def test_a_rejected_action_cannot_be_approved_again(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = (await _propose(client, task_id)).json()
    await client.post(f"/actions/{action['id']}/reject")

    response = await client.post(f"/actions/{action['id']}/approval-request")

    assert response.status_code == 409


# ---- execution --------------------------------------------------------------


async def test_starting_an_attempt_consumes_the_approval_and_persists_the_intent(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)

    action = await _executing_action(client, task_id)

    assert action["status"] == "EXECUTING"
    assert action["approval"] is None  # Consumed, so it is no longer live.
    assert len(action["attempts"]) == 1
    attempt = action["attempts"][0]
    assert attempt["attempt_number"] == 1
    assert attempt["finished_at"] is None
    assert attempt["outcome"] is None
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "EXECUTING"


async def test_an_unapproved_action_cannot_start_an_attempt(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = (await _propose(client, task_id)).json()

    response = await client.post(f"/actions/{action['id']}/attempts")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_action_transition"
    assert (await client.get(f"/actions/{action['id']}")).json()["attempts"] == []


async def test_a_consumed_approval_cannot_fund_a_second_attempt(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)

    replay = await client.post(f"/actions/{action['id']}/attempts")

    assert replay.status_code == 409
    assert len((await client.get(f"/actions/{action['id']}")).json()["attempts"]) == 1


async def test_finishing_with_success_resolves_the_action_and_frees_the_task(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)

    response = await client.post(
        f"/actions/{action['id']}/attempts/finish",
        json={"outcome": "SUCCEEDED", "result": {"booking_reference": "BK-1"}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["attempts"][0]["result"] == {"booking_reference": "BK-1"}
    # A succeeding action does not make the whole task SUCCEEDED.
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "READY"
    assert await _event_types(client, task_id) == [
        "task.created",
        "action.proposed",
        "action.approval_requested",
        "action.approved",
        "action.execution_started",
        "action.succeeded",
    ]


async def test_finishing_with_failure_records_a_known_failure(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)

    response = await client.post(
        f"/actions/{action['id']}/attempts/finish",
        json={"outcome": "FAILED", "error_code": "slot_taken"},
    )

    assert response.json()["status"] == "FAILED"
    assert response.json()["attempts"][0]["error_code"] == "slot_taken"
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "READY"
    assert "action.failed" in await _event_types(client, task_id)


async def test_an_executor_may_report_that_it_does_not_know(
    client: httpx.AsyncClient,
) -> None:
    """A lost response is not a failure: it is an unknown outcome."""
    task_id = await _task(client)
    action = await _executing_action(client, task_id)

    response = await client.post(
        f"/actions/{action['id']}/attempts/finish",
        json={"outcome": "OUTCOME_UNKNOWN", "error_code": "response_lost"},
    )

    assert response.json()["status"] == "OUTCOME_UNKNOWN"
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "OUTCOME_UNKNOWN"
    assert "action.outcome_unknown" in await _event_types(client, task_id)


async def test_finishing_an_action_that_is_not_executing_is_refused(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _approved_action(client, task_id)

    response = await client.post(
        f"/actions/{action['id']}/attempts/finish", json={"outcome": "SUCCEEDED"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_action_transition"


async def test_finishing_twice_is_refused(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)
    await client.post(
        f"/actions/{action['id']}/attempts/finish", json={"outcome": "SUCCEEDED"}
    )

    response = await client.post(
        f"/actions/{action['id']}/attempts/finish", json={"outcome": "FAILED"}
    )

    assert response.status_code == 409
    assert (await client.get(f"/actions/{action['id']}")).json()["status"] == "SUCCEEDED"


# ---- reconciliation ---------------------------------------------------------


async def _unknown_action(client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
    action = await _executing_action(client, task_id)
    response = await client.post(
        f"/actions/{action['id']}/attempts/finish", json={"outcome": "OUTCOME_UNKNOWN"}
    )
    return _body(response)


@pytest.mark.parametrize(
    ("result", "expected_action_status", "expected_task_status"),
    [
        ("SUCCEEDED", "SUCCEEDED", "READY"),
        ("FAILED", "FAILED", "READY"),
        ("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"),
    ],
)
async def test_reconciliation_records_the_authoritative_result(
    client: httpx.AsyncClient,
    result: str,
    expected_action_status: str,
    expected_task_status: str,
) -> None:
    task_id = await _task(client)
    action = await _unknown_action(client, task_id)

    action = (
        await client.post(
            f"/actions/{action['id']}/reconciliation",
            json={"expected_revision": action["revision"]},
        )
    ).json()
    assert action["status"] == "RECONCILING"

    response = await client.post(
        f"/actions/{action['id']}/reconciliation/finish",
        json={"result": result, "expected_revision": action["revision"]},
    )

    assert response.status_code == 200
    assert response.json()["status"] == expected_action_status
    assert (await client.get(f"/tasks/{task_id}")).json()["status"] == expected_task_status
    # Reconciliation looks; it never acts. There is still exactly one attempt.
    assert len(response.json()["attempts"]) == 1
    timeline = await _event_types(client, task_id)
    assert timeline[-2:] == ["action.reconciliation_started", "action.reconciled"]


async def test_an_inconclusive_reconciliation_can_be_repeated(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _unknown_action(client, task_id)

    for _ in range(2):
        action = (await client.post(f"/actions/{action['id']}/reconciliation")).json()
        action = (
            await client.post(
                f"/actions/{action['id']}/reconciliation/finish",
                json={"result": "OUTCOME_UNKNOWN"},
            )
        ).json()
        assert action["status"] == "OUTCOME_UNKNOWN"

    final = (
        await client.post(f"/actions/{action['id']}/reconciliation")
    ).json()
    final = (
        await client.post(
            f"/actions/{final['id']}/reconciliation/finish", json={"result": "SUCCEEDED"}
        )
    ).json()
    assert final["status"] == "SUCCEEDED"
    assert len(final["attempts"]) == 1


async def test_reconciliation_cannot_run_on_a_known_outcome(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)
    await client.post(
        f"/actions/{action['id']}/attempts/finish", json={"outcome": "SUCCEEDED"}
    )

    response = await client.post(f"/actions/{action['id']}/reconciliation")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_action_transition"


async def test_reconciliation_cannot_run_on_an_action_that_is_still_executing(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)

    response = await client.post(f"/actions/{action['id']}/reconciliation")

    assert response.status_code == 409


async def test_finishing_reconciliation_that_never_began_is_refused(
    client: httpx.AsyncClient,
) -> None:
    task_id = await _task(client)
    action = await _unknown_action(client, task_id)

    response = await client.post(
        f"/actions/{action['id']}/reconciliation/finish", json={"result": "SUCCEEDED"}
    )

    assert response.status_code == 409


# ---- input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_tier": "R9"},
        {"tool_name": "Commit Booking"},
        {"idempotency_key": ""},
        {"proposal": "not an object"},
        {"proposal": {"note": "x" * 70_000}},
    ],
)
async def test_malformed_proposals_are_rejected(
    client: httpx.AsyncClient, overrides: dict[str, Any]
) -> None:
    task_id = await _task(client)
    assert (await _propose(client, task_id, **overrides)).status_code == 422


async def test_an_unknown_outcome_value_is_rejected(client: httpx.AsyncClient) -> None:
    task_id = await _task(client)
    action = await _executing_action(client, task_id)

    response = await client.post(
        f"/actions/{action['id']}/attempts/finish", json={"outcome": "PROBABLY"}
    )

    assert response.status_code == 422
