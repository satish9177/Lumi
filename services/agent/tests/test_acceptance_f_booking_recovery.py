"""Milestone 10 S5, Acceptance F: an uncertain booking after a hard kill, across every route that could repeat it.

Nothing is simulated except the site, which is the reviewed local booking fixture (no real booking is ever
made). A real Chromium in a real browser-worker process submits the booking once; the fixture holds the
response; the runtime AND the worker are then hard-killed (TerminateProcess, no shutdown hook); a fresh
runtime and worker start against the same database and the same site.

What must be true, and is asserted here:

    after restart         one action, one attempt, one consequential dispatch, the ORIGINAL ids,
                          action and task OUTCOME_UNKNOWN, no approval left to spend
    while unresolved      the same task, a new booking task, the generic action API and the generic
                          attempt route are all refused before any browser is involved (the shared lock;
                          the other executors -- project, file, desktop, workflow, other provider/model --
                          are driven against the same lock in the service suites, see
                          test_effect_registry.py and the S5 sections of the executor suites)
    a second restart      recovers nothing twice (one outcome_unknown event)
    reconciliation        read-only lookup: FOUND -> SUCCEEDED on the same action; authoritative absence
                          (the fixture's reviewed declaration) -> FAILED, and a retry is a NEW action with a
                          NEW exact approval
    the site              exactly one booking effect per approved action, ever

A NON-authoritative "not found" cannot be produced by this fixture (its declaration makes absence
authoritative, on purpose); that branch is proven against the same reconciliation code with a scripted
lookup in test_effect_registry.py.
"""

import threading
import time
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
SLOT_B = booking_proposal("slot-b-1915", "Dr B", "2026-09-19T19:15:00+05:30", 950)


def _fire_and_forget(http: RuntimeHttp, url: str) -> None:
    def call() -> None:
        try:
            http.post(url, timeout=300)
        except Exception:  # noqa: BLE001 - the process dies underneath it.
            pass

    threading.Thread(target=call, daemon=True).start()


def _dispatches(http: RuntimeHttp, action_id: str) -> list[dict[str, Any]]:
    body = ok(http.get(f"{http.base_url}/actions/{action_id}/browser-dispatches", timeout=30))
    dispatches: list[dict[str, Any]] = body["dispatches"]
    return dispatches


def _events(http: RuntimeHttp, task_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = ok(http.get(f"{http.base_url}/tasks/{task_id}/events?limit=500", timeout=30))["events"]
    return events


def _wait_for_submission(control: SiteControl, reference: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if control.state()["submissions_by_reference"].get(reference, 0) >= 1:
            return
        time.sleep(0.1)
    pytest.fail(f"the fixture never received the submission: {control.state()}")


def _book_and_kill(
    tmp_path: Path, database_url: str, site_url: str, control: SiteControl, runtime_port: int, *, before_commit: bool
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    """Approve SLOT_A, dispatch once, hold the response, hard-kill runtime and worker. Returns ids and the
    original attempt/dispatch as the dying runtime last recorded them."""
    with browser_worker(tmp_path / "worker-1.log", site_origin=site_url) as worker:
        with runtime(
            tmp_path / "runtime-1.log", database_url=database_url, port=runtime_port,
            worker_url=worker.base_url, worker_token=worker.token, worker_timeout_seconds=300.0,
        ) as first:
            action = drive_to_approved(first.http, SLOT_A)
            action_id, task_id = action["id"], action["task_id"]
            reference = f"lumi-{action_id}"
            fault = "drop_response_before_commit_for_reference" if before_commit else "drop_response_for_reference"
            control.set_faults(**{fault: reference, "drop_response_mode": "hang"})
            _fire_and_forget(first.http, f"{first.base_url}/actions/{action_id}/browser-execution")
            if before_commit:
                _wait_for_submission(control, reference)
            else:
                control.wait_for_bookings(1)
            executing = ok(first.http.get(f"{first.base_url}/actions/{action_id}", timeout=30))
            assert executing["status"] == "EXECUTING" and len(executing["attempts"]) == 1
            (dispatch,) = _dispatches(first.http, action_id)
            assert dispatch["status"] == "DISPATCHED" and dispatch["effect"] == "CONSEQUENTIAL"
            attempt = executing["attempts"][0]
        # Leaving both blocks hard-kills the runtime, then the worker.
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{runtime_port}/health", timeout=2)
    return action_id, task_id, attempt, dispatch


def _assert_recovered_unknown(api: RuntimeHttp, action_id: str, task_id: str, attempt: dict[str, Any], dispatch: dict[str, Any]) -> None:
    recovered = ok(api.get(f"{api.base_url}/actions/{action_id}", timeout=30))
    assert recovered["status"] == "OUTCOME_UNKNOWN"
    assert ok(api.get(f"{api.base_url}/tasks/{task_id}", timeout=30))["status"] == "OUTCOME_UNKNOWN"
    assert [item["id"] for item in recovered["attempts"]] == [attempt["id"]]
    assert recovered["attempts"][0]["outcome"] == "OUTCOME_UNKNOWN"
    assert recovered["approval"] is None
    dispatches = _dispatches(api, action_id)
    assert [item["id"] for item in dispatches] == [dispatch["id"]]
    assert dispatches[0]["status"] == "OUTCOME_UNKNOWN"


def _attack_while_unresolved(api: RuntimeHttp, action_id: str, task_id: str, control: SiteControl) -> None:
    """Every booking-reachable route that could repeat the effect is refused before any browser acts."""
    base_url = api.base_url
    submissions = control.state()["submissions"]
    # Same task: a second booking beside the unknown one.
    same = api.post(f"{base_url}/tasks/{task_id}/booking/prepare", json={"slot_id": SLOT_B["slot_id"]}, timeout=120)
    assert same.status_code == 409, same.text
    # The original action: no generic attempt, no re-approval, no browser re-execution.
    assert api.post(f"{base_url}/actions/{action_id}/attempts", timeout=30).status_code == 422
    assert api.post(f"{base_url}/actions/{action_id}/approval-request", timeout=30).status_code == 409
    assert api.post(f"{base_url}/actions/{action_id}/browser-execution", timeout=60).status_code == 409
    # The generic action API cannot mint a booking at all.
    minted = api.post(
        f"{base_url}/tasks/{task_id}/actions",
        json={"idempotency_key": "bypass", "tool_name": "commit_booking", "risk_tier": "R2", "proposal": SLOT_B},
        timeout=30,
    )
    assert minted.status_code == 422 and minted.json()["error"]["code"] == "effect_route_refused"
    # A NEW task, freshly and exactly approved, for a different slot: refused by the shared lock.
    fresh = drive_to_approved(api, SLOT_B)
    blocked = api.post(f"{base_url}/actions/{fresh['id']}/browser-execution", timeout=120)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "effect_locked", blocked.text
    after = ok(api.get(f"{base_url}/actions/{fresh['id']}", timeout=30))
    # M10 S5 review finding 2: the approval given while the booking was unknown is withdrawn, never kept.
    assert after["status"] == "REJECTED" and after["attempts"] == []
    assert _dispatches(api, fresh["id"]) == []
    # Nothing above reached the site.
    assert control.state()["submissions"] == submissions


def _restart_again(tmp_path: Path, database_url: str, site_url: str, runtime_port: int, name: str) -> None:
    """A second restart with nothing in between: it must not recover, record or retry anything again."""
    with browser_worker(tmp_path / f"worker-{name}.log", site_origin=site_url) as worker:
        with runtime(
            tmp_path / f"runtime-{name}.log", database_url=database_url, port=runtime_port,
            worker_url=worker.base_url, worker_token=worker.token,
        ):
            pass


def test_acceptance_f_a_booking_confirmed_before_the_crash_is_found_never_repeated(
    migrated_database_url: str, tmp_path: Path
) -> None:
    truncate_all(migrated_database_url)
    runtime_port = free_port()
    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)
        action_id, task_id, attempt, dispatch = _book_and_kill(
            tmp_path, migrated_database_url, site.base_url, control, runtime_port, before_commit=False
        )
        reference = f"lumi-{action_id}"
        assert control.state()["booking_count"] == 1

        with browser_worker(tmp_path / "worker-2.log", site_origin=site.base_url) as worker:
            with runtime(
                tmp_path / "runtime-2.log", database_url=migrated_database_url, port=runtime_port,
                worker_url=worker.base_url, worker_token=worker.token,
            ) as second:
                _assert_recovered_unknown(second.http, action_id, task_id, attempt, dispatch)
                _attack_while_unresolved(second.http, action_id, task_id, control)

        _restart_again(tmp_path, migrated_database_url, site.base_url, runtime_port, "3")

        with browser_worker(tmp_path / "worker-4.log", site_origin=site.base_url) as worker:
            with runtime(
                tmp_path / "runtime-4.log", database_url=migrated_database_url, port=runtime_port,
                worker_url=worker.base_url, worker_token=worker.token,
            ) as fourth:
                api = fourth.http
                _assert_recovered_unknown(api, action_id, task_id, attempt, dispatch)
                control.set_faults()
                final = ok(api.post(f"{api.base_url}/actions/{action_id}/browser-reconciliation", timeout=120))
                events = _events(api, task_id)
                dispatches = _dispatches(api, action_id)

        state = control.state()
    assert final["id"] == action_id and final["status"] == "SUCCEEDED"
    assert [item["id"] for item in final["attempts"]] == [attempt["id"]]
    consequential = [row for row in dispatches if row["effect"] == "CONSEQUENTIAL"]
    lookups = [row for row in dispatches if row["effect"] == "READ_ONLY"]
    assert [row["id"] for row in consequential] == [dispatch["id"]]
    assert len(lookups) == 1 and lookups[0]["attempt_id"] is None and lookups[0]["submitted"] is False
    # Exactly one external effect, ever, for this approval -- and for every refused attempt, none.
    assert state["booking_count"] == 1
    assert state["submissions"] == 1 and state["submissions_by_reference"] == {reference: 1}
    # Recovered exactly once across three restarts.
    assert [event["event_type"] for event in events].count("action.outcome_unknown") == 1
    reconciled = events[-1]
    assert reconciled["event_type"] == "action.reconciled"
    assert reconciled["payload"]["evidence"]["lookup"] == "FOUND"
    assert reconciled["payload"]["evidence"]["booking_count"] == 1


def test_acceptance_f_a_booking_lost_before_commit_is_authoritatively_absent_and_retried_only_by_a_new_approval(
    migrated_database_url: str, tmp_path: Path
) -> None:
    truncate_all(migrated_database_url)
    runtime_port = free_port()
    with fixture_site(tmp_path / "site.log") as site:
        control = SiteControl(site.base_url)
        action_id, task_id, attempt, dispatch = _book_and_kill(
            tmp_path, migrated_database_url, site.base_url, control, runtime_port, before_commit=True
        )
        reference = f"lumi-{action_id}"
        assert control.state()["booking_count"] == 0  # the submission arrived; nothing was created

        with browser_worker(tmp_path / "worker-2.log", site_origin=site.base_url) as worker:
            with runtime(
                tmp_path / "runtime-2.log", database_url=migrated_database_url, port=runtime_port,
                worker_url=worker.base_url, worker_token=worker.token,
            ) as second:
                api = second.http
                _assert_recovered_unknown(api, action_id, task_id, attempt, dispatch)
                _attack_while_unresolved(api, action_id, task_id, control)
                control.set_faults()
                settled = ok(api.post(f"{api.base_url}/actions/{action_id}/browser-reconciliation", timeout=120))
                assert settled["status"] == "FAILED"
                assert [item["id"] for item in settled["attempts"]] == [attempt["id"]]
                # The consumed approval is gone for good: the same action can never run again.
                assert api.post(f"{api.base_url}/actions/{action_id}/browser-execution", timeout=60).status_code == 409
                assert api.post(f"{api.base_url}/actions/{action_id}/approval-request", timeout=30).status_code == 409
                # A retry is a NEW action in the same task with its own exact approval.
                retry = ok(api.post(f"{api.base_url}/tasks/{task_id}/booking/prepare", json={"slot_id": SLOT_A["slot_id"]}, timeout=120))
                assert retry["id"] != action_id and retry["status"] == "WAITING_APPROVAL"
                retry = ok(api.post(f"{api.base_url}/actions/{retry['id']}/approve", json={"expected_revision": retry["revision"]}, timeout=30))
                done = ok(api.post(f"{api.base_url}/actions/{retry['id']}/browser-execution", timeout=180))
                assert done["status"] == "SUCCEEDED"
                events = _events(api, task_id)

        state = control.state()
    reconciled = next(e for e in events if e["event_type"] == "action.reconciled" and e["payload"]["action_id"] == action_id)
    assert reconciled["payload"]["result"] == "FAILED"
    assert reconciled["payload"]["evidence"]["lookup"] == "NOT_FOUND"
    assert reconciled["payload"]["evidence"]["absence_is_authoritative"] is True
    # One booking, made by the NEW approval; the lost submission created nothing and was never repeated.
    assert state["booking_count"] == 1
    assert state["bookings"][0]["reference"] == f"lumi-{done['id']}"
    assert state["submissions_by_reference"] == {reference: 1, f"lumi-{done['id']}": 1}
