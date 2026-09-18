"""Acceptance: a public research task through the real durable pipeline.

Real processes throughout: the Lumi runtime (the same `app.server` entry point
Electron uses), an isolated browser worker with Chromium, the research fixture
site, and a canary nothing may contact. Every assertion that matters is checked
twice -- in PostgreSQL, and in the fixture's own request log.

Covered here: the multi-hop happy path (search -> open a result -> follow a link
-> ground an answer); nothing runs before the trusted grant; the scope card;
stale refs after a worker restart; the endless-corridor budget stop; hostile
page content gaining no capability; and a runtime crash mid-step.

The planner is *this test*: it chooses each step the way Electron main's planner
would, which is the point -- the runtime's behaviour must not depend on a model.
"""

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

from app.browser.session import generate_worker_token
from evals.sites.public_pages.app import DECOYS, PROJECT, RESEARCH_INJECTION
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

OBJECTIVE = "Find the Lumi project page and tell me how many contributors it has"
DISCLOSURE = {"recipients": ["scripted"], "max_text_chars": 10_000}


@dataclass
class Sites:
    site: PublicSiteControl
    canary: PublicSiteControl


@pytest.fixture(scope="module")
def sites(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sites]:
    logs = tmp_path_factory.mktemp("research-sites")
    with public_fixture_site(logs / "site.log") as site, public_fixture_site(
        logs / "canary.log"
    ) as canary:
        yield Sites(PublicSiteControl(site.base_url), PublicSiteControl(canary.base_url))


@pytest.fixture(autouse=True)
def _reset(sites: Sites, migrated_database_url: str) -> None:
    truncate_all(migrated_database_url)
    sites.site.reset()
    sites.canary.reset()


def _worker(tmp_path: Path, sites: Sites, name: str = "worker.log", port: int | None = None) -> Any:
    return browser_worker(
        tmp_path / name,
        site_origin="http://127.0.0.1:9",
        research_test_origins=sites.site.base_url,
        port=port,
    )


def _runtime(
    tmp_path: Path,
    database_url: str,
    worker: WorkerProcess,
    sites: Sites,
    *,
    name: str = "runtime.log",
    port: int | None = None,
) -> Any:
    return runtime(
        tmp_path / name,
        database_url=database_url,
        port=port,
        worker_url=worker.base_url,
        worker_token=worker.token,
        worker_timeout_seconds=300.0,
        extra_environment={
            "LUMI_RESEARCH_TEST_ORIGINS": sites.site.base_url,
            # The runtime's own bounded JSON GET. No credential, no browser.
            "LUMI_RESEARCH_SEARCH_ENDPOINT": f"{sites.site.base_url}/search?q={{query}}",
        },
    )


class Planner:
    """Stands in for Electron main's planner: chooses one step at a time."""

    def __init__(self, http: RuntimeHttp, task_id: str) -> None:
        self.http = http
        self.task_id = task_id
        self.calls = 0

    def step(self, step: dict[str, Any], *, request_id: str | None = None) -> httpx.Response:
        self.calls += 1
        return self.http.post(
            f"{self.http.base_url}/tasks/{self.task_id}/research/steps",
            json={
                "request_id": request_id or f"req-{self.calls:08d}",
                "step": step,
                "planner_calls": self.calls,
            },
            timeout=300,
        )

    def ok_step(self, step: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        body = ok(self.step(step, **kwargs))
        assert body["outcome"] == "SUCCEEDED", (body["error_code"], body["observation"])
        return body

    def view(self) -> dict[str, Any]:
        return ok(self.http.get(f"{self.http.base_url}/tasks/{self.task_id}/research", timeout=30))


def _task(http: RuntimeHttp, objective: str = OBJECTIVE) -> str:
    task = ok(
        http.post(
            f"{http.base_url}/tasks",
            json={
                "request": {
                    "type": "public_research",
                    "text": objective,
                    "objective": objective,
                    "source": "text",
                }
            },
            timeout=30,
        )
    )
    task_id: str = task["id"]
    return task_id


def _prepare(http: RuntimeHttp, task_id: str, **budgets: int) -> dict[str, Any]:
    body: dict[str, Any] = {"disclosure": DISCLOSURE}
    if budgets:
        body["budgets"] = budgets
    return ok(
        http.post(f"{http.base_url}/tasks/{task_id}/research/prepare", json=body, timeout=30)
    )


def _grant(http: RuntimeHttp, task_id: str, card: dict[str, Any]) -> dict[str, Any]:
    grant = card["grant"]
    return ok(
        http.post(
            f"{http.base_url}/tasks/{task_id}/research/grant",
            json={"grant_id": grant["id"], "expected_revision": grant["revision"]},
            timeout=30,
        )
    )


def _ledger(database_url: str) -> dict[str, int]:
    import asyncio

    async def run() -> dict[str, int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:

                async def count(sql: str) -> int:
                    return int(await connection.scalar(text(sql)) or 0)

                return {
                    "grants": await count("SELECT count(*) FROM task_grants"),
                    "active_grants": await count(
                        "SELECT count(*) FROM task_grants WHERE status = 'ACTIVE'"
                    ),
                    "authorizations": await count("SELECT count(*) FROM step_authorizations"),
                    "consumed": await count(
                        "SELECT count(*) FROM step_authorizations WHERE consumed_at IS NOT NULL"
                    ),
                    "approvals": await count("SELECT count(*) FROM approvals"),
                    "attempts": await count("SELECT count(*) FROM action_attempts"),
                    "scoped_attempts": await count(
                        "SELECT count(*) FROM action_attempts WHERE step_authorization_id IS NOT NULL"
                    ),
                    "observations": await count("SELECT count(*) FROM research_observations"),
                    "sessions": await count("SELECT count(*) FROM research_sessions"),
                    "open_sessions": await count(
                        "SELECT count(*) FROM research_sessions WHERE status = 'OPEN'"
                    ),
                    "stale_sessions": await count(
                        "SELECT count(*) FROM research_sessions WHERE status = 'STALE'"
                    ),
                    "answers": await count("SELECT count(*) FROM research_answers"),
                    "dispatches": await count("SELECT count(*) FROM browser_dispatches"),
                }
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _events(http: RuntimeHttp, task_id: str) -> list[str]:
    body = ok(
        http.get(
            f"{http.base_url}/tasks/{task_id}/events?after_sequence=0&limit=200", timeout=30
        )
    )
    return [event["event_type"] for event in body["events"]]


def _blocks(observation: dict[str, Any]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = observation["blocks"]
    return blocks


def _find_block(observation: dict[str, Any], needle: str) -> dict[str, str]:
    return next(block for block in _blocks(observation) if needle in block["text"])


# ---- the happy path ---------------------------------------------------------------


def test_a_granted_research_task_searches_navigates_follows_and_answers(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    """Search -> open one result -> follow one semantic link -> grounded answer."""
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)

        # Nothing may happen before the trusted click, not even a search.
        refused = planner.step({"operation": "public_search", "query": "lumi project"})
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "research_grant_not_found"

        card = _prepare(http, task_id)
        scope = card["grant"]["scope"]
        assert card["grant"]["status"] == "PENDING"
        assert card["grant"]["expires_at"] is None
        assert "public_search" in scope["allowed_operations"]
        assert scope["methods"] == ["GET", "HEAD"]
        for forbidden in ("login", "forms_and_typing", "uploads_and_downloads", "private_network"):
            assert forbidden in scope["forbidden"]

        pending = planner.step({"operation": "public_search", "query": "lumi project"})
        assert pending.status_code == 409
        assert pending.json()["error"]["code"] == "research_grant_not_usable"
        assert _ledger(migrated_database_url)["observations"] == 0

        granted = _grant(http, task_id, card)
        assert granted["grant"]["status"] == "ACTIVE"
        assert granted["grant"]["expires_at"] is not None

        search = planner.ok_step({"operation": "public_search", "query": "lumi project"})
        results = search["observation"]["results"]
        assert search["observation"]["kind"] == "search_results"
        assert search["observation"]["ref"] == "o1"
        assert len(results) >= 3
        hub = next(result for result in results if "directory" in result["title"].lower())

        opened = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": hub["id"]},
            }
        )
        hub_observation = opened["observation"]
        assert hub_observation["kind"] == "page"
        assert hub_observation["final_url"].endswith("/research/hub")
        # The hub holds no statistics; the fact is one hop away.
        assert PROJECT["contributors"] not in " ".join(
            block["text"] for block in _blocks(hub_observation)
        )
        link = next(
            entry for entry in hub_observation["links"] if "lumi-desktop" in entry["text"]
        )
        # What crosses the boundary is a ref, a label and a host -- not an address.
        assert set(link) == {"id", "text", "host"}

        followed = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {
                    "kind": "link",
                    "observation": hub_observation["ref"],
                    "ref": link["id"],
                },
            }
        )
        project = followed["observation"]
        assert project["final_url"].endswith("/research/project")
        evidence_block = _find_block(project, PROJECT["contributors"])

        answered = ok(
            http.post(
                f"{http.base_url}/tasks/{task_id}/research/answer",
                json={
                    "answer": {
                        "status": "answered",
                        "stop_reason": "goal_reached",
                        "answer": f"The Lumi desktop project lists {PROJECT['contributors']} contributors.",
                        "evidence": [
                            {
                                "observation": project["ref"],
                                "block": evidence_block["id"],
                                "quote": evidence_block["text"],
                            }
                        ],
                    },
                    "provider": "scripted",
                    "model": "scripted-research",
                    "planner_calls": planner.calls,
                },
                timeout=30,
            )
        )
        assert answered["answer"]["status"] == "answered"
        assert answered["grant"]["status"] == "COMPLETED"
        assert answered["task"]["status"] == "SUCCEEDED"
        assert answered["session"] is None

        ledger = _ledger(migrated_database_url)
        assert ledger["grants"] == 1
        assert ledger["authorizations"] == ledger["consumed"] == 3
        assert ledger["attempts"] == ledger["scoped_attempts"] == 3
        # Not one exact approval was created: the scope was confirmed once.
        assert ledger["approvals"] == 0
        assert ledger["observations"] == 3
        assert ledger["sessions"] == 1 and ledger["open_sessions"] == 0
        assert ledger["answers"] == 1
        # Search is the runtime's own request, so only the browser steps have
        # a dispatch row.
        assert ledger["dispatches"] == 2

        events = _events(http, task_id)
        assert events.count("action.authorized") == 3
        assert "action.approved" not in events
        assert "task.research_scope_requested" in events
        assert "task.research_scope_granted" in events
        assert "task.research_answer_recorded" in events

        hits = sites.site.hits()
        assert hits.get("/search", 0) == 1
        assert hits.get("/research/hub", 0) == 1
        assert hits.get("/research/project", 0) == 1
        assert sites.canary.hits() == {}


def test_one_browser_session_serves_the_whole_task(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        _grant(http, task_id, _prepare(http, task_id))
        planner.ok_step({"operation": "public_search", "query": "lumi project"})
        first = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": "r1"},
            }
        )
        second = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": "r2"},
            }
        )
        assert first["observation"]["session_id"] == second["observation"]["session_id"]
        # History exists only because the context survived between steps.
        back = planner.ok_step({"operation": "history", "tab": "t1", "direction": "back"})
        assert back["observation"]["final_url"].endswith("/research/hub")
        assert _ledger(migrated_database_url)["sessions"] == 1


def test_a_decoy_page_cannot_ground_an_answer_about_the_real_one(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    """The distractor fixture: the right label on the wrong page proves nothing."""
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        _grant(http, task_id, _prepare(http, task_id))
        planner.ok_step({"operation": "public_search", "query": "lumi project"})
        decoy = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": "r2"},
            }
        )
        block = _find_block(decoy["observation"], DECOYS["grinder"]["stars"])
        refused = http.post(
            f"{http.base_url}/tasks/{task_id}/research/answer",
            json={
                "answer": {
                    "status": "answered",
                    "stop_reason": "goal_reached",
                    # The number is the decoy's; the quote is the decoy's too,
                    # but the claim is about contributors of the real project.
                    "answer": f"The Lumi desktop project lists {PROJECT['contributors']} contributors.",
                    "evidence": [
                        {
                            "observation": decoy["observation"]["ref"],
                            "block": block["id"],
                            "quote": block["text"],
                        }
                    ],
                },
                "provider": "scripted",
                "model": "scripted-research",
                "planner_calls": planner.calls,
            },
            timeout=30,
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "research_answer_not_grounded"
        assert refused.json()["error"]["reason"] == "number_not_in_evidence"
        assert _ledger(migrated_database_url)["answers"] == 0


def test_hostile_page_content_gains_no_capability_and_no_authorization(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        card = _prepare(http, task_id)
        _grant(http, task_id, card)
        planner.ok_step({"operation": "public_search", "query": "lumi mirror"})
        step = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": "r4"},
            }
        )
        observation = step["observation"]
        text_seen = " ".join(block["text"] for block in _blocks(observation))
        assert RESEARCH_INJECTION[:40] in text_seen
        assert observation["provenance"] == "untrusted_environment"

        # The scope is exactly what it was, and no second grant appeared.
        view = planner.view()
        assert view["grant"]["scope_digest"] == card["grant"]["scope_digest"]
        assert view["grant"]["scope"]["allowed_operations"] == card["grant"]["scope"][
            "allowed_operations"
        ]
        ledger = _ledger(migrated_database_url)
        assert ledger["grants"] == 1 and ledger["approvals"] == 0
        # Nothing it asked for reached anything.
        assert sites.canary.hits() == {}
        assert not [key for key in sites.site.hits() if "mutation-canary" in key]
        # The page said 999. An answer may only cite what a cited block shows.
        assert observation["open_tabs"] == ["t1"]


def test_an_operation_outside_the_vocabulary_is_refused_with_a_stable_code(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        _grant(http, task_id, _prepare(http, task_id))
        for step in (
            {"operation": "click", "selector": "a"},
            {"operation": "navigate", "tab": "t1", "url": f"{sites.site.base_url}/research/project"},
            {"operation": "evaluate", "script": "fetch('/x')"},
            {"operation": "observe", "tab": "t1", "selector": ".stats"},
        ):
            refused = planner.step(step)
            assert refused.status_code == 422, step
            assert refused.json()["error"]["code"] == "research_step_refused"
            assert refused.json()["error"]["reason"] == "unsupported_operation"
        assert _ledger(migrated_database_url)["observations"] == 0
        assert sites.site.hits() == {}


def test_the_step_budget_stops_an_endless_corridor(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    """The budget fixture: `/research/loop/n` always offers a next page."""
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        objective = f"Walk {sites.site.base_url}/research/loop/1 until you find the answer"
        task_id = _task(http, objective)
        planner = Planner(http, task_id)
        card = _prepare(http, task_id, max_steps=4)
        assert card["grant"]["scope"]["seeds"] == [f"{sites.site.base_url}/research/loop/1"]
        _grant(http, task_id, card)

        step = planner.ok_step(
            {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}}
        )
        walked = 1
        while True:
            observation = step["observation"]
            following = next(
                entry for entry in observation["links"] if entry["text"] == "Next page"
            )
            response = planner.step(
                {
                    "operation": "navigate",
                    "tab": "t1",
                    "target": {
                        "kind": "link",
                        "observation": observation["ref"],
                        "ref": following["id"],
                    },
                }
            )
            if response.status_code == 409:
                assert response.json()["error"]["code"] == "research_budget_exhausted"
                assert response.json()["error"]["reason"] == "max_steps"
                break
            step = ok(response)
            walked += 1
            assert walked <= 10, "the budget did not stop the loop"
        assert walked == 4
        assert _ledger(migrated_database_url)["observations"] == 4

        # The honest ending: a partial answer, not another step.
        partial = ok(
            http.post(
                f"{http.base_url}/tasks/{task_id}/research/answer",
                json={
                    "answer": {
                        "status": "not_found",
                        "stop_reason": "budget_exhausted",
                        "answer": "Lumi stopped after its step limit without finding that.",
                    },
                    "provider": "scripted",
                    "model": "scripted-research",
                    "planner_calls": planner.calls,
                },
                timeout=30,
            )
        )
        assert partial["answer"]["stop_reason"] == "budget_exhausted"
        assert partial["grant"]["status"] == "COMPLETED"


def test_stopping_the_task_revokes_the_scope_and_drops_the_session(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        _grant(http, task_id, _prepare(http, task_id))
        planner.ok_step({"operation": "public_search", "query": "lumi project"})
        planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": "r1"},
            }
        )
        stopped = ok(
            http.post(
                f"{http.base_url}/tasks/{task_id}/research/revoke",
                json={"reason": "user_stopped"},
                timeout=30,
            )
        )
        assert stopped["grant"]["status"] == "REVOKED"
        assert stopped["session"] is None
        assert _ledger(migrated_database_url)["open_sessions"] == 0
        refused = planner.step({"operation": "observe", "tab": "t1"})
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "research_grant_not_usable"
        assert "task.research_scope_revoked" in _events(http, task_id)


def test_a_replayed_step_request_executes_nothing_twice(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    with _worker(tmp_path, sites) as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        _grant(http, task_id, _prepare(http, task_id))
        planner.ok_step({"operation": "public_search", "query": "lumi project"})
        step = {
            "operation": "navigate",
            "tab": "t1",
            "target": {"kind": "result", "observation": "o1", "ref": "r1"},
        }
        first = planner.ok_step(step, request_id="req-duplicate")
        again = ok(planner.step(step, request_id="req-duplicate"))
        assert again["replayed"] is True
        assert again["action"]["id"] == first["action"]["id"]
        assert sites.site.hits().get("/research/hub", 0) == 1
        ledger = _ledger(migrated_database_url)
        assert ledger["authorizations"] == 2 and ledger["observations"] == 2


# ---- recovery ----------------------------------------------------------------------


def test_a_worker_restart_makes_every_semantic_ref_stale(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    """A ref means a document in a context. Both are gone with the process.

    The *runtime* keeps running here, so this is a worker restart and nothing
    else: the same task, the same grant, the same planner, and a browser that
    has no memory of the document the ref belongs to.
    """
    worker_port = free_port()
    token = generate_worker_token()
    worker_one = browser_worker(
        tmp_path / "worker.log",
        site_origin="http://127.0.0.1:9",
        research_test_origins=sites.site.base_url,
        token=token,
        port=worker_port,
    )
    with worker_one as worker, _runtime(
        tmp_path, migrated_database_url, worker, sites
    ) as process:
        http = process.http
        task_id = _task(http)
        planner = Planner(http, task_id)
        _grant(http, task_id, _prepare(http, task_id))
        planner.ok_step({"operation": "public_search", "query": "lumi project"})
        hub = planner.ok_step(
            {
                "operation": "navigate",
                "tab": "t1",
                "target": {"kind": "result", "observation": "o1", "ref": "r1"},
            }
        )
        link = next(
            entry for entry in hub["observation"]["links"] if "lumi-desktop" in entry["text"]
        )
        worker.kill()
        sites.site.reset()

        # A new worker process on the same endpoint, with the same credential
        # and a brand new generation.
        with browser_worker(
            tmp_path / "worker-2.log",
            site_origin="http://127.0.0.1:9",
            research_test_origins=sites.site.base_url,
            token=token,
            port=worker_port,
        ):
            stale = planner.step(
                {
                    "operation": "navigate",
                    "tab": "t1",
                    "target": {
                        "kind": "link",
                        "observation": hub["observation"]["ref"],
                        "ref": link["id"],
                    },
                }
            )
            assert stale.status_code == 422
            assert stale.json()["error"]["code"] == "research_step_refused"
            assert stale.json()["error"]["reason"] == "stale_session"
            assert sites.site.hits() == {}
            ledger = _ledger(migrated_database_url)
            assert ledger["stale_sessions"] >= 1

            # Re-observing is how the task learns where it actually is. The
            # fresh session has a fresh tab, so the honest answer is
            # "this tab holds nothing yet" -- never the old observation.
            reobserved = planner.ok_step({"operation": "observe", "tab": "t1"})
            assert reobserved["observation"]["kind"] == "tab_state"
            assert (
                reobserved["observation"]["session_id"]
                != hub["observation"]["session_id"]
            )


def test_a_runtime_crash_mid_step_leaves_an_unknown_and_no_blind_repeat(
    tmp_path: Path, migrated_database_url: str, sites: Sites
) -> None:
    port = free_port()
    with _worker(tmp_path, sites) as worker:
        with _runtime(tmp_path, migrated_database_url, worker, sites, port=port) as process:
            http = process.http
            task_id = _task(http)
            planner = Planner(http, task_id)
            _grant(http, task_id, _prepare(http, task_id))
            planner.ok_step({"operation": "public_search", "query": "lumi project"})

            # A step against a page that never answers, killed once the fixture
            # has seen the request.
            slow = threading.Thread(
                target=lambda: planner.step(
                    {
                        "operation": "navigate",
                        "tab": "t1",
                        "target": {"kind": "result", "observation": "o1", "ref": "r1"},
                    }
                ),
                daemon=True,
            )
            sites.site.reset()
            slow.start()
            sites.site.wait_for_hit("/research/hub")
            time.sleep(0.3)
            process.kill()

        with _runtime(
            tmp_path, migrated_database_url, worker, sites, name="runtime-2.log", port=port
        ) as restarted:
            http = restarted.http
            view = ok(http.get(f"{http.base_url}/tasks/{task_id}/research", timeout=30))
            # The step the dead runtime started is unknown, never "failed", and
            # nothing was repeated on its behalf.
            statuses = [
                action["status"]
                for action in ok(
                    http.get(f"{http.base_url}/tasks/{task_id}/actions?limit=100", timeout=30)
                )["actions"]
                if action["tool_name"].startswith("research_")
            ]
            assert "OUTCOME_UNKNOWN" in statuses
            assert view["answer"] is None
            ledger = _ledger(migrated_database_url)
            assert ledger["stale_sessions"] >= 1
            # An unresolved step blocks the next one rather than racing it.
            planner = Planner(http, task_id)
            planner.calls = 5
            refused = planner.step({"operation": "observe", "tab": "t1"})
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "research_step_in_flight"
