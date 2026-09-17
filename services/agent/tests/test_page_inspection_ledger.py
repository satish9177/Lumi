"""Acceptance: an approved page inspection through the real durable pipeline.

Real processes throughout: the Lumi runtime (the same `app.server` entry point
Electron uses), an isolated browser worker with Chromium, the public-page
fixture, and a canary site nothing may contact. Every assertion that matters is
checked twice -- in PostgreSQL, and in the fixture's own request log.

Covered here: the happy path; exact single-use approval (unapproved, stale
revision, consumed, concurrent); hostile content gaining no capability; a
forbidden redirect; and the fault cases -- kill before dispatch, worker crash
mid-read, runtime crash mid-read, and a caller that loses the response after the
observation was stored.
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from evals.sites.public_pages import PROFILE
from tests.browser_harness import (
    PublicSiteControl,
    RuntimeHttp,
    WorkerProcess,
    browser_worker,
    free_port,
    ok,
    public_fixture_site,
    runtime,
)
from tests.conftest import truncate_all

pytestmark = [pytest.mark.browser, pytest.mark.hardkill]

QUESTION = "What is my contest rating?"
DISCLOSURE = {"recipients": ["scripted"], "max_text_chars": 12_000}


@dataclass
class Sites:
    site: PublicSiteControl
    canary: PublicSiteControl


@pytest.fixture(scope="module")
def sites(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sites]:
    logs = tmp_path_factory.mktemp("inspection-sites")
    with public_fixture_site(logs / "site.log") as site, public_fixture_site(logs / "canary.log") as canary:
        yield Sites(PublicSiteControl(site.base_url), PublicSiteControl(canary.base_url))


@pytest.fixture(autouse=True)
def _reset(sites: Sites, migrated_database_url: str) -> None:
    truncate_all(migrated_database_url)
    sites.site.reset()
    sites.canary.reset()


def _worker(tmp_path: Path, sites: Sites, name: str = "worker.log", port: int | None = None) -> Any:
    return browser_worker(
        tmp_path / name, site_origin="http://127.0.0.1:9",
        inspection_test_origins=sites.site.base_url, port=port,
    )


def _runtime(tmp_path: Path, database_url: str, worker: WorkerProcess, sites: Sites, *,
             name: str = "runtime.log", port: int | None = None) -> Any:
    return runtime(
        tmp_path / name, database_url=database_url, port=port,
        worker_url=worker.base_url, worker_token=worker.token, worker_timeout_seconds=300.0,
        extra_environment={"LUMI_INSPECTION_TEST_ORIGINS": sites.site.base_url},
    )


def _card(http: RuntimeHttp, url: str) -> dict[str, Any]:
    task = ok(http.post(
        f"{http.base_url}/tasks",
        json={"request": {"type": "page_inspection", "text": QUESTION, "url": url, "question": QUESTION}},
        timeout=30,
    ))
    card = ok(http.post(f"{http.base_url}/tasks/{task['id']}/inspection/prepare", json={"disclosure": DISCLOSURE}, timeout=30))
    assert card["status"] == "WAITING_APPROVAL"
    return card


def _approve(http: RuntimeHttp, card: dict[str, Any]) -> dict[str, Any]:
    approved = ok(http.post(f"{http.base_url}/actions/{card['id']}/approve", json={"expected_revision": card["revision"]}, timeout=30))
    assert approved["status"] == "APPROVED"
    return approved


def _execute(http: RuntimeHttp, action: dict[str, Any], timeout: float = 300) -> httpx.Response:
    return http.post(
        f"{http.base_url}/actions/{action['id']}/browser-execution",
        json={"expected_revision": action["revision"]}, timeout=timeout,
    )


def _inspection(http: RuntimeHttp, action_id: str) -> dict[str, Any]:
    return ok(http.get(f"{http.base_url}/actions/{action_id}/inspection", timeout=30))


def _counts(database_url: str) -> dict[str, int]:
    async def run() -> dict[str, int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                async def count(sql: str) -> int:
                    return int(await connection.scalar(text(sql)) or 0)

                return {
                    "tasks": await count("SELECT count(*) FROM tasks"),
                    "actions": await count("SELECT count(*) FROM actions"),
                    "approvals": await count("SELECT count(*) FROM approvals"),
                    "attempts": await count("SELECT count(*) FROM action_attempts"),
                    "dispatches": await count("SELECT count(*) FROM browser_dispatches"),
                    "observations": await count("SELECT count(*) FROM page_observations"),
                }
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _in_background(call: Any) -> list[Any]:
    done: list[Any] = []

    def target() -> None:
        try:
            done.append(call())
        except Exception as error:  # noqa: BLE001 - the process may die underneath it.
            done.append(error)

    threading.Thread(target=target, daemon=True).start()
    return done


def test_an_approved_inspection_reads_once_and_stores_one_bound_observation(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/profiles/rated"
    with _worker(tmp_path, sites) as worker, _runtime(tmp_path, migrated_database_url, worker, sites) as rt:
        http = rt.http
        card = _card(http, url)
        # Nothing is read before the trusted approval.
        assert sites.site.hits() == {}
        refused = _execute(http, card)
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, "invalid_action_transition")
        assert sites.site.hits() == {}

        done = ok(_execute(http, _approve(http, card)))
        assert done["status"] == "SUCCEEDED"
        attempt = done["attempts"][0]
        assert attempt["outcome"] == "SUCCEEDED" and attempt["result"]["submitted"] is False

        view = _inspection(http, card["id"])
        observation = view["observation"]
        assert observation["requested_url"] == observation["final_url"] == url
        assert observation["attempt_id"] == attempt["id"]
        assert observation["content_hash"] == attempt["result"]["content_hash"]
        assert observation["worker_generation"] == worker.identity()["worker_generation"]
        texts = [block["text"] for block in observation["blocks"]]
        assert texts[texts.index("Contest rating") + 1] == PROFILE["contest_rating"]

        dispatches = ok(http.get(f"{http.base_url}/actions/{card['id']}/browser-dispatches", timeout=30))["dispatches"]
        assert [(d["operation"], d["site"], d["effect"], d["status"], d["submitted"]) for d in dispatches] == [
            ("inspect_public_page", "public_web", "READ_ONLY", "OK", False)
        ]
        assert sites.site.hits() == {"/profiles/rated": 1}

        # The consumed approval cannot fund a second read.
        again = _execute(http, done)
        assert again.status_code == 409
        assert sites.site.hits() == {"/profiles/rated": 1}
    assert _counts(migrated_database_url) == {
        "tasks": 1, "actions": 1, "approvals": 1, "attempts": 1, "dispatches": 1, "observations": 1,
    }


def test_a_stale_revision_and_a_concurrent_second_execution_read_nothing_extra(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/profiles/dynamic"
    with _worker(tmp_path, sites) as worker, _runtime(tmp_path, migrated_database_url, worker, sites) as rt:
        http = rt.http
        card = _card(http, url)
        approved = _approve(http, card)
        stale = _execute(http, card)  # the revision the card had before approval
        assert (stale.status_code, stale.json()["error"]["code"]) == (409, "stale_action_revision")
        assert ok(http.get(f"{http.base_url}/actions/{card['id']}", timeout=30))["approval"]["status"] == "APPROVED"

        first = _in_background(lambda: _execute(http, approved))
        second = _in_background(lambda: _execute(http, approved))
        deadline = time.monotonic() + 300
        while (not first or not second) and time.monotonic() < deadline:
            time.sleep(0.1)
        statuses = sorted(response.status_code for response in (*first, *second))
        assert statuses == [200, 409]
    assert sites.site.hits().get("/profiles/dynamic") == 1
    counts = _counts(migrated_database_url)
    assert (counts["attempts"], counts["dispatches"], counts["observations"]) == (1, 1, 1)


def test_hostile_page_content_creates_no_action_approval_or_request(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/profiles/hostile?canary={sites.canary.base_url}"
    with _worker(tmp_path, sites) as worker, _runtime(tmp_path, migrated_database_url, worker, sites) as rt:
        http = rt.http
        card = _card(http, url)
        done = ok(_execute(http, _approve(http, card)))
        assert done["status"] == "SUCCEEDED"
        events = ok(http.get(f"{http.base_url}/tasks/{card['task_id']}/events", timeout=30))["events"]
        assert [event["event_type"] for event in events] == [
            "task.created", "action.proposed", "action.approval_requested", "action.approved",
            "action.execution_started", "action.succeeded",
        ]
        assert "IGNORE" not in str(events).upper()
    assert sites.canary.hits() == {}
    assert _counts(migrated_database_url) == {
        "tasks": 1, "actions": 1, "approvals": 1, "attempts": 1, "dispatches": 1, "observations": 1,
    }


def test_a_forbidden_redirect_is_a_known_failure_with_nothing_stored(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/redirect/to?target={sites.canary.base_url}/profiles/rated"
    with _worker(tmp_path, sites) as worker, _runtime(tmp_path, migrated_database_url, worker, sites) as rt:
        http = rt.http
        card = _card(http, url)
        done = ok(_execute(http, _approve(http, card)))
        assert done["status"] == "FAILED"
        attempt = done["attempts"][0]
        assert attempt["error_code"] == "redirect_blocked"
        assert attempt["result"]["redirect_refusal"] == "https_required"
        assert _inspection(http, card["id"])["observation"] is None
    assert sites.canary.hits() == {}
    assert _counts(migrated_database_url)["observations"] == 0


def test_killed_after_claim_before_dispatch_is_unknown_after_restart_and_needs_new_approval(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/profiles/rated"
    port = free_port()
    with _worker(tmp_path, sites) as worker:
        with _runtime(tmp_path, migrated_database_url, worker, sites, port=port) as first:
            card = _card(first.http, url)
            approved = _approve(first.http, card)
            # The approval is claimed and the intent persisted; the dispatch never goes out.
            ok(first.http.post(f"{first.base_url}/actions/{card['id']}/attempts",
                               json={"expected_revision": approved["revision"]}, timeout=30))
            first.kill()
        with _runtime(tmp_path, migrated_database_url, worker, sites, name="runtime-2.log", port=port) as second:
            http = second.http
            recovered = ok(http.get(f"{http.base_url}/actions/{card['id']}", timeout=30))
            assert recovered["status"] == "OUTCOME_UNKNOWN"
            assert recovered["attempts"][0]["error_code"] == "runtime_restart"
            assert _inspection(http, card["id"])["observation"] is None
            # Never retried: the consumed approval cannot be used again...
            assert _execute(http, recovered).status_code == 409
            # ...and a repeat is a new action behind a new exact approval.
            repeat = ok(http.post(f"{http.base_url}/tasks/{card['task_id']}/inspection/prepare",
                                  json={"disclosure": DISCLOSURE}, timeout=30))
            assert repeat["id"] != card["id"] and repeat["approval"]["status"] == "PENDING"
    assert sites.site.hits() == {}


def test_a_worker_that_dies_mid_read_leaves_an_unknown_not_a_failure(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/slow"
    with _worker(tmp_path, sites) as worker, _runtime(tmp_path, migrated_database_url, worker, sites) as rt:
        http = rt.http
        card = _card(http, url)
        approved = _approve(http, card)
        responses = _in_background(lambda: _execute(http, approved))
        sites.site.wait_for_hit("/slow")
        worker.kill()
        deadline = time.monotonic() + 120
        while not responses and time.monotonic() < deadline:
            time.sleep(0.1)
        action = ok(responses[0])
        assert action["status"] == "OUTCOME_UNKNOWN"
        assert action["attempts"][0]["error_code"] == "browser_worker_lost_response"
        assert _inspection(http, card["id"])["observation"] is None
        assert _execute(http, action).status_code == 409
    assert _counts(migrated_database_url)["observations"] == 0


def test_a_runtime_that_dies_mid_read_is_recovered_as_unknown(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/slow"
    port = free_port()
    with _worker(tmp_path, sites) as worker:
        with _runtime(tmp_path, migrated_database_url, worker, sites, port=port) as first:
            card = _card(first.http, url)
            approved = _approve(first.http, card)
            _in_background(lambda: _execute(first.http, approved))
            sites.site.wait_for_hit("/slow")
            first.kill()
        with _runtime(tmp_path, migrated_database_url, worker, sites, name="runtime-2.log", port=port) as second:
            http = second.http
            view = _inspection(http, card["id"])
            assert view["action"]["status"] == "OUTCOME_UNKNOWN"
            assert view["observation"] is None
            dispatches = ok(http.get(f"{http.base_url}/actions/{card['id']}/browser-dispatches", timeout=30))["dispatches"]
            assert [(d["status"], d["error_code"]) for d in dispatches] == [("OUTCOME_UNKNOWN", "runtime_restart")]
            answer = http.post(
                f"{http.base_url}/actions/{card['id']}/inspection/answer",
                json={"observation_id": card["id"], "content_hash": "0" * 64, "provider": "scripted", "model": "m",
                      "answer": {"status": "not_found", "answer": "Could not verify this from the inspected page."}},
                timeout=30,
            )
            assert answer.json()["error"]["code"] == "observation_not_available"


def test_an_observation_stored_after_the_caller_gave_up_is_still_there_to_answer_from(
    migrated_database_url: str, sites: Sites, tmp_path: Path
) -> None:
    url = f"{sites.site.base_url}/profiles/dynamic"
    with _worker(tmp_path, sites) as worker, _runtime(tmp_path, migrated_database_url, worker, sites) as rt:
        http = rt.http
        card = _card(http, url)
        approved = _approve(http, card)
        # The caller (Electron main, in production) stops waiting long before
        # the read finishes: the response is lost, the work is not.
        with pytest.raises(httpx.TimeoutException):
            _execute(http, approved, timeout=0.5)
        deadline = time.monotonic() + 120
        view = _inspection(http, card["id"])
        while view["action"]["status"] == "EXECUTING" and time.monotonic() < deadline:
            time.sleep(0.2)
            view = _inspection(http, card["id"])
        assert view["action"]["status"] == "SUCCEEDED"
        observation = view["observation"]
        texts = [block["text"] for block in observation["blocks"]]
        rating_block = f"b{texts.index(PROFILE['contest_rating']) + 1}"
        recorded = ok(http.post(
            f"{http.base_url}/actions/{card['id']}/inspection/answer",
            json={"observation_id": observation["id"], "content_hash": observation["content_hash"],
                  "provider": "scripted", "model": "scripted-rules",
                  "answer": {"status": "answered", "answer": f"Your contest rating is {PROFILE['contest_rating']}.",
                             "evidence": [{"block": rating_block, "quote": PROFILE["contest_rating"]}]}},
            timeout=30,
        ))
        assert recorded["answer"]["status"] == "answered"
    assert sites.site.hits().get("/profiles/dynamic") == 1
