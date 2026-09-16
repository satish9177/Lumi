"""Acceptance: the booking succeeded, the answer was lost, everything died.

This is the scenario Milestones 2 and 3 exist for, and the only version of it
worth trusting is the one where nothing is simulated:

* a real Chromium fills a real form and presses a real button;
* the fixture site really creates the booking;
* the response is really never delivered;
* the runtime and the browser worker are really hard-killed, mid-flight, with no
  shutdown hook and no chance to record anything;
* a fresh runtime and a fresh worker start up and have to work out what happened
  from the database and the site alone.

The kill is deterministic rather than timed: the test waits until the *site*
reports that the booking exists, and only then kills. So "the booking succeeded
but Lumi never learned" is guaranteed, not hoped for.

What must come out the other side:

    1 execution attempt      (never 2, never rewritten into a failure)
    1 browser submission     (the button was pressed once)
    1 booking                (and no duplicate)
    action SUCCEEDED         (established by looking, never by guessing)
"""

import threading
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.browser_harness import (
    RuntimeHttp,
    SiteControl,
    booking_proposal,
    browser_worker,
    drive_to_approved,
    fixture_site,
    free_port,
    ok,
    runtime,
)
from tests.conftest import truncate_all

pytestmark = [pytest.mark.browser, pytest.mark.hardkill]

SLOT_A = booking_proposal("slot-a-1830", "Dr A", "2026-09-19T18:30:00+05:30", 800)


def _fire_and_forget(http: RuntimeHttp, url: str, done: list[Any]) -> threading.Thread:
    """Start the execution and do not wait for it. It is never going to answer."""

    def call() -> None:
        try:
            done.append(http.post(url, timeout=300))
        except Exception as error:  # noqa: BLE001 - the process dies underneath it.
            done.append(error)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread


def _dispatches(http: RuntimeHttp, action_id: str) -> list[dict[str, Any]]:
    body = ok(http.get(f"{http.base_url}/actions/{action_id}/browser-dispatches", timeout=30))
    dispatches: list[dict[str, Any]] = body["dispatches"]
    return dispatches


def test_a_booking_that_succeeded_before_everything_died_is_reconciled_not_repeated(
    migrated_database_url: str, tmp_path: Path
) -> None:
    truncate_all(migrated_database_url)
    runtime_port, worker_port = free_port(), free_port()

    # 1-3. The site outlives everything: it is the authoritative record, and a
    # test that restarted it would be proving nothing.
    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)

        with browser_worker(tmp_path / "worker-1.log", site_origin=site.base_url,
                            port=worker_port) as first_worker:
            first_worker_generation = first_worker.identity()["worker_generation"]

            with runtime(
                tmp_path / "runtime-1.log",
                database_url=migrated_database_url,
                port=runtime_port,
                worker_url=first_worker.base_url,
                worker_token=first_worker.token,
                # Generous, so the kill below is what ends this execution --
                # not a client timeout that would make the test prove something
                # easier than it claims to.
                worker_timeout_seconds=300.0,
            ) as first_runtime:
                # 4-7. Approve the exact booking.
                action = drive_to_approved(first_runtime.http, SLOT_A)
                action_id = action["id"]
                task_id = action["task_id"]
                reference = f"lumi-{action_id}"

                # 8. The site will create the booking and then never answer.
                control.set_faults(
                    drop_response_for_reference=reference, drop_response_mode="hang"
                )

                # 9-10. Execute. The call persists the attempt, dispatches to the
                # worker, and then hangs forever waiting for the browser.
                responses: list[Any] = []
                _fire_and_forget(
                    first_runtime.http, f"{first_runtime.base_url}/actions/{action_id}/browser-execution", responses
                )

                # 11. Wait for proof that the side effect really happened.
                state = control.wait_for_bookings(1)
                assert state["bookings"][0]["reference"] == reference
                assert state["submissions_by_reference"][reference] == 1

                # The ledger is mid-flight: the attempt is durable, its outcome
                # is not yet known to anyone.
                executing = ok(
                    first_runtime.http.get(f"{first_runtime.base_url}/actions/{action_id}", timeout=30)
                )
                assert executing["status"] == "EXECUTING"
                assert len(executing["attempts"]) == 1
                original_attempt = executing["attempts"][0]
                assert original_attempt["finished_at"] is None
                assert executing["approval"] is None  # Claimed by this attempt.
                dispatched = _dispatches(first_runtime.http, action_id)
                assert len(dispatched) == 1
                assert dispatched[0]["status"] == "DISPATCHED"
                assert dispatched[0]["attempt_id"] == original_attempt["id"]
                original_dispatch = dispatched[0]

            # 12. Hard kill, both processes, no graceful anything. (The context
            # managers kill on exit; the runtime is already gone here.)
            assert responses == [] or isinstance(responses[0], Exception)

        with pytest.raises(httpx.TransportError):
            httpx.get(f"http://127.0.0.1:{runtime_port}/health", timeout=2)

        # 13. Fresh processes. A new worker means a new worker generation, so
        # nothing the dead one might still say could be accepted anyway.
        with browser_worker(tmp_path / "worker-2.log", site_origin=site.base_url) as second_worker:
            second_worker_generation = second_worker.identity()["worker_generation"]
            assert second_worker_generation != first_worker_generation

            with runtime(
                tmp_path / "runtime-2.log",
                database_url=migrated_database_url,
                port=runtime_port,
                worker_url=second_worker.base_url,
                worker_token=second_worker.token,
            ) as second_runtime:
                base_url = second_runtime.base_url
                api = second_runtime.http

                # 14. Startup recovery refuses to guess.
                recovered = ok(api.get(f"{base_url}/actions/{action_id}", timeout=30))
                assert recovered["status"] == "OUTCOME_UNKNOWN"
                assert ok(api.get(f"{base_url}/tasks/{task_id}", timeout=30))["status"] == (
                    "OUTCOME_UNKNOWN"
                )

                # 15. Still exactly one attempt. It was not rewritten into a
                # failure, and no second one was created to "try again".
                assert len(recovered["attempts"]) == 1
                attempt = recovered["attempts"][0]
                assert attempt["id"] == original_attempt["id"]
                assert attempt["attempt_number"] == 1
                assert attempt["outcome"] == "OUTCOME_UNKNOWN"
                assert attempt["error_code"] == "runtime_restart"
                assert recovered["approval"] is None  # Nothing to retry with.

                # The orphaned browser dispatch was closed as an unknown too.
                after_recovery = _dispatches(api, action_id)
                assert len(after_recovery) == 1
                assert after_recovery[0]["id"] == original_dispatch["id"]
                assert after_recovery[0]["status"] == "OUTCOME_UNKNOWN"
                assert after_recovery[0]["error_code"] == "runtime_restart"

                # And the site still holds exactly one booking: recovery looked
                # at the database only, and touched nothing outside it.
                assert control.state()["booking_count"] == 1

                # 16-19. Reconciliation: a read-only lookup through the browser.
                final = ok(
                    api.post(
                        f"{base_url}/actions/{action_id}/browser-reconciliation", timeout=120
                    )
                )
                assert final["status"] == "SUCCEEDED"
                assert ok(api.get(f"{base_url}/tasks/{task_id}", timeout=30))["status"] == "READY"

                events = ok(api.get(f"{base_url}/tasks/{task_id}/events", timeout=30))["events"]
                dispatches = _dispatches(api, action_id)

        # 20. The counts this milestone is judged on.
        state = control.state()
        assert state["booking_count"] == 1, "a duplicate booking was created"
        assert state["submissions"] == 1, "the button was pressed more than once"
        assert state["submissions_by_reference"] == {reference: 1}
        assert state["bookings"][0]["booking_id"] == "BK-0001"
        assert state["bookings"][0]["price"] == 800

    assert len(final["attempts"]) == 1
    assert final["attempts"][0]["id"] == original_attempt["id"]

    # Exactly one dispatch ever asked a browser to change the world; the second
    # is the read-only lookup, which is not an execution attempt.
    consequential = [row for row in dispatches if row["effect"] == "CONSEQUENTIAL"]
    lookups = [row for row in dispatches if row["effect"] == "READ_ONLY"]
    assert len(consequential) == 1
    assert consequential[0]["id"] == original_dispatch["id"]
    assert len(lookups) == 1
    assert lookups[0]["operation"] == "lookup_booking"
    assert lookups[0]["attempt_id"] is None
    assert lookups[0]["submitted"] is False
    # The lookup ran on the new worker; the consequential dispatch is still
    # attributed to the one that died.
    assert lookups[0]["worker_generation"] == second_worker_generation
    assert consequential[0]["worker_generation"] == first_worker_generation

    # The timeline tells the whole story, in order, with no invented steps.
    assert [event["event_type"] for event in events] == [
        "task.created",
        "action.proposed",
        "action.approval_requested",
        "action.approved",
        "action.execution_started",
        "action.outcome_unknown",
        "action.reconciliation_started",
        "action.reconciled",
    ]
    unknown = next(e for e in events if e["event_type"] == "action.outcome_unknown")
    assert unknown["payload"]["reason"] == "runtime_restart"
    assert unknown["payload"]["browser_dispatch_id"] == original_dispatch["id"]

    reconciled = events[-1]
    assert reconciled["payload"]["result"] == "SUCCEEDED"
    evidence = reconciled["payload"]["evidence"]
    assert evidence["source"] == "browser_lookup"
    assert evidence["lookup"] == "FOUND"
    assert evidence["reference"] == reference
    assert evidence["booking_id"] == "BK-0001"
    assert evidence["booking_count"] == 1  # Not two. There is no duplicate.


def test_a_recovered_browser_action_cannot_be_executed_again(
    migrated_database_url: str, tmp_path: Path
) -> None:
    """After recovery the action is closed to every path back into a browser."""
    truncate_all(migrated_database_url)
    runtime_port = free_port()

    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)
        with browser_worker(tmp_path / "worker-1.log", site_origin=site.base_url) as worker_one:
            with runtime(
                tmp_path / "runtime-1.log",
                database_url=migrated_database_url,
                port=runtime_port,
                worker_url=worker_one.base_url,
                worker_token=worker_one.token,
                worker_timeout_seconds=300.0,
            ) as first:
                action = drive_to_approved(first.http, SLOT_A)
                action_id = action["id"]
                reference = f"lumi-{action_id}"
                control.set_faults(
                    drop_response_for_reference=reference, drop_response_mode="hang"
                )
                _fire_and_forget(
                    first.http, f"{first.base_url}/actions/{action_id}/browser-execution", []
                )
                control.wait_for_bookings(1)

        with browser_worker(tmp_path / "worker-2.log", site_origin=site.base_url) as worker_two:
            with runtime(
                tmp_path / "runtime-2.log",
                database_url=migrated_database_url,
                port=runtime_port,
                worker_url=worker_two.base_url,
                worker_token=worker_two.token,
            ) as second:
                base_url = second.base_url
                api = second.http
                retry_browser = api.post(
                    f"{base_url}/actions/{action_id}/browser-execution", timeout=60
                )
                retry_attempt = api.post(f"{base_url}/actions/{action_id}/attempts", timeout=30)
                reapprove = api.post(
                    f"{base_url}/actions/{action_id}/approval-request", timeout=30
                )
                current = ok(api.get(f"{base_url}/actions/{action_id}", timeout=30))

        assert retry_browser.status_code == 409
        assert retry_attempt.status_code == 409
        assert reapprove.status_code == 409
        assert current["status"] == "OUTCOME_UNKNOWN"
        assert len(current["attempts"]) == 1
        # Every refusal happened before a browser was involved.
        state = control.state()
        assert state["booking_count"] == 1
        assert state["submissions"] == 1


def test_a_worker_killed_after_submitting_leaves_an_unknown_not_a_failure(
    migrated_database_url: str, tmp_path: Path
) -> None:
    """Only the browser dies. The runtime survives and must still not guess.

    The runtime's dispatch was delivered, so a dropped connection proves nothing
    about whether the booking exists -- and here it does exist.
    """
    truncate_all(migrated_database_url)

    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)
        with browser_worker(tmp_path / "worker-1.log", site_origin=site.base_url) as worker_one:
            with runtime(
                tmp_path / "runtime.log",
                database_url=migrated_database_url,
                worker_url=worker_one.base_url,
                worker_token=worker_one.token,
                worker_timeout_seconds=300.0,
            ) as lumi:
                action = drive_to_approved(lumi.http, SLOT_A)
                action_id = action["id"]
                reference = f"lumi-{action_id}"
                control.set_faults(
                    drop_response_for_reference=reference, drop_response_mode="hang"
                )

                responses: list[Any] = []
                thread = _fire_and_forget(
                    lumi.http, f"{lumi.base_url}/actions/{action_id}/browser-execution", responses
                )
                control.wait_for_bookings(1)

                # Kill only the browser worker.
                worker_one.kill()
                thread.join(timeout=120)
                assert responses, "the execution call never returned"

                executed = ok(lumi.http.get(f"{lumi.base_url}/actions/{action_id}", timeout=30))
                assert executed["status"] == "OUTCOME_UNKNOWN"
                attempt = executed["attempts"][0]
                assert attempt["outcome"] == "OUTCOME_UNKNOWN"
                assert len(executed["attempts"]) == 1

            # A new worker, and a runtime that can reach it, resolve the unknown.
            with browser_worker(tmp_path / "worker-2.log", site_origin=site.base_url) as worker_two:
                with runtime(
                    tmp_path / "runtime-2.log",
                    database_url=migrated_database_url,
                    worker_url=worker_two.base_url,
                    worker_token=worker_two.token,
                ) as lumi_two:
                    final = ok(
                        lumi_two.http.post(
                            f"{lumi_two.base_url}/actions/{action_id}/browser-reconciliation",
                            timeout=120,
                        )
                    )

        assert final["status"] == "SUCCEEDED"
        assert len(final["attempts"]) == 1
        state = control.state()
        assert state["booking_count"] == 1
        assert state["submissions_by_reference"] == {reference: 1}


def test_a_worker_timeout_after_submission_is_not_a_failure(
    migrated_database_url: str, tmp_path: Path
) -> None:
    """Nothing crashes. The worker simply waits too long, having already clicked.

    A timeout is a statement about how long we waited, never about whether a
    booking exists. This one must come out `OUTCOME_UNKNOWN`, and the booking it
    was unsure about really is there.
    """
    truncate_all(migrated_database_url)

    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)
        with browser_worker(
            tmp_path / "worker.log",
            site_origin=site.base_url,
            # Short, so the worker's own operation timeout is what fires.
            operation_timeout_seconds=5.0,
        ) as worker:
            with runtime(
                tmp_path / "runtime.log",
                database_url=migrated_database_url,
                worker_url=worker.base_url,
                worker_token=worker.token,
                worker_timeout_seconds=120.0,
            ) as lumi:
                action = drive_to_approved(lumi.http, SLOT_A)
                action_id = action["id"]
                reference = f"lumi-{action_id}"
                control.set_faults(
                    drop_response_for_reference=reference, drop_response_mode="hang"
                )

                executed = ok(
                    lumi.http.post(
                        f"{lumi.base_url}/actions/{action_id}/browser-execution", timeout=180
                    )
                )
                assert executed["status"] == "OUTCOME_UNKNOWN"
                attempt = executed["attempts"][0]
                assert attempt["outcome"] == "OUTCOME_UNKNOWN"
                # The worker knew it had already clicked, and said so.
                assert attempt["error_code"] == "timeout_after_submission"
                assert attempt["result"]["submitted"] is True

                # The booking really does exist, which is why FAILED would have
                # been wrong.
                assert control.state()["booking_count"] == 1

                control.set_faults()
                final = ok(
                    lumi.http.post(
                        f"{lumi.base_url}/actions/{action_id}/browser-reconciliation", timeout=120
                    )
                )

        assert final["status"] == "SUCCEEDED"
        assert len(final["attempts"]) == 1
        assert control.state()["submissions_by_reference"] == {reference: 1}


def test_a_stale_worker_result_is_refused(migrated_database_url: str, tmp_path: Path) -> None:
    """A dispatch addressed to a worker generation that no longer exists.

    The runtime binds to a worker generation at handshake time. If that worker
    is replaced between the handshake and the dispatch, the new one refuses:
    consequential work is never done by a process the runtime did not address.
    """
    truncate_all(migrated_database_url)
    port = free_port()

    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)
        token = None
        with browser_worker(
            tmp_path / "worker-1.log", site_origin=site.base_url, port=port
        ) as worker_one:
            token = worker_one.token
            first_generation = worker_one.identity()["worker_generation"]

        # A different process on the same port, with the same credential, but a
        # new identity. A credential alone is not enough to accept its answers.
        with browser_worker(
            tmp_path / "worker-2.log", site_origin=site.base_url, port=port, token=token
        ) as worker_two:
            second_generation = worker_two.identity()["worker_generation"]
            assert second_generation != first_generation

            response = httpx.post(
                f"{worker_two.base_url}/v1/dispatch",
                headers={"x-lumi-worker-token": token.get_secret_value()},
                json={
                    "dispatch_id": str(uuid.uuid4()),
                    "runtime_generation": str(uuid.uuid4()),
                    "expected_worker_generation": first_generation,
                    "action_id": str(uuid.uuid4()),
                    "attempt_id": str(uuid.uuid4()),
                    "operation": "commit_booking",
                    "site": "appointment_fixture",
                    "input": {"reference": "lumi-stale", "proposal": SLOT_A},
                },
                timeout=60,
            )

        assert response.status_code == 409
        assert response.json()["code"] == "stale_worker_generation"
        # Refused before any browser work: nothing was submitted.
        assert control.state()["submissions"] == 0
