"""The research operations against a real Chromium and the research fixtures.

A real worker process, a real browser, and two fixture processes: the site the
worker may read, and a *canary* it may not. The canary's request log is the
evidence that a refused destination was never contacted -- not merely that Lumi
reported refusing it.

These tests drive the worker directly, with synthetic action and attempt ids.
The authorization path is covered in test_research_authorization.py and the
end-to-end path in test_research_ledger.py.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.browser.client import BrowserWorkerClient
from app.browser.errors import BrowserWorkerRejectedError
from app.browser.operations.research import HISTORY, NAVIGATE, OBSERVE, SCROLL, TAB
from app.browser.protocol import (
    DispatchRequest,
    DispatchResponse,
    OperationStatus,
    SessionRequest,
)
from app.domain.research import (
    MAX_LINKS,
    PUBLIC_RESEARCH_SITE,
    ResearchObservation,
)
from evals.sites.public_pages.app import DECOYS, PROJECT, RESEARCH_INJECTION
from tests.browser_harness import (
    PublicSiteControl,
    WorkerProcess,
    browser_worker,
    public_fixture_site,
)

pytestmark = [pytest.mark.browser]


class Research:
    """One research session in the worker, driven step by step."""

    def __init__(
        self, site: PublicSiteControl, canary: PublicSiteControl, worker: WorkerProcess
    ) -> None:
        self.site = site
        self.canary = canary
        self.worker = worker
        self.runtime_generation = uuid.uuid4()
        self.sequence = 0

    def _client(self) -> BrowserWorkerClient:
        return BrowserWorkerClient(
            base_url=self.worker.base_url,
            token=self.worker.token,
            runtime_generation=self.runtime_generation,
            timeout_seconds=120,
        )

    async def open_session(self, session_id: uuid.UUID | None = None) -> uuid.UUID:
        chosen = session_id or uuid.uuid4()
        client = self._client()
        try:
            identity = await client.identify()
            await client.open_session(
                SessionRequest(
                    session_id=chosen,
                    runtime_generation=self.runtime_generation,
                    expected_worker_generation=identity.worker_generation,
                )
            )
        finally:
            await client.aclose()
        return chosen

    async def close_session(self, session_id: uuid.UUID) -> str:
        client = self._client()
        try:
            identity = await client.identify()
            answer = await client.close_session(
                SessionRequest(
                    session_id=session_id,
                    runtime_generation=self.runtime_generation,
                    expected_worker_generation=identity.worker_generation,
                )
            )
            return answer.status
        finally:
            await client.aclose()

    async def step(
        self,
        session_id: uuid.UUID | None,
        operation: str,
        payload: dict[str, Any],
        **overrides: Any,
    ) -> DispatchResponse:
        self.sequence += 1
        client = self._client()
        try:
            identity = await client.identify()
            request = DispatchRequest(
                **{
                    "dispatch_id": uuid.uuid4(),
                    "runtime_generation": self.runtime_generation,
                    "expected_worker_generation": identity.worker_generation,
                    "action_id": uuid.uuid4(),
                    "attempt_id": uuid.uuid4(),
                    "operation": operation,
                    "site": PUBLIC_RESEARCH_SITE,
                    "session_id": session_id,
                    "input": {"sequence": self.sequence, **payload},
                    **overrides,
                }
            )
            return await client.dispatch(request)
        finally:
            await client.aclose()

    async def navigate(
        self, session_id: uuid.UUID, url: str, *, tab: str = "t1", **payload: Any
    ) -> DispatchResponse:
        return await self.step(
            session_id,
            NAVIGATE,
            {"tab": tab, "url": url, "target_kind": "seed", **payload},
        )


@pytest.fixture(scope="module")
def research(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Research]:
    logs: Path = tmp_path_factory.mktemp("research-pages")
    with public_fixture_site(logs / "site.log") as site, public_fixture_site(
        logs / "canary.log"
    ) as canary:
        with browser_worker(
            logs / "worker.log",
            site_origin="http://127.0.0.1:9",
            research_test_origins=site.base_url,
            research_max_tabs=3,
        ) as worker:
            yield Research(
                PublicSiteControl(site.base_url), PublicSiteControl(canary.base_url), worker
            )


@pytest.fixture(autouse=True)
def _clean_logs(research: Research) -> None:
    research.site.reset()
    research.canary.reset()


def _observation(response: DispatchResponse) -> ResearchObservation:
    assert response.status is OperationStatus.OK, (response.error_code, response.observation)
    assert response.submitted is False
    return ResearchObservation.model_validate(response.observation["observation"])


def _targets(response: DispatchResponse) -> dict[str, str]:
    targets: dict[str, str] = response.observation.get("targets", {})
    return targets


def _text(observation: ResearchObservation) -> str:
    return "\n".join(block.text for block in observation.blocks)


# ---- the session persists across steps -------------------------------------------


async def test_a_session_is_reused_across_steps_and_keeps_its_history(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        hub = _observation(await research.navigate(session, f"{research.site.base_url}/research/hub"))
        assert hub.kind == "page"
        assert hub.session_id == session
        assert hub.tab == "t1"
        assert hub.open_tabs == ["t1"]
        project = _observation(
            await research.navigate(session, f"{research.site.base_url}/research/project")
        )
        assert project.document_epoch > hub.document_epoch
        # History only exists because the context survived between steps.
        back = _observation(await research.step(session, HISTORY, {"tab": "t1", "direction": "back"}))
        assert back.final_url is not None and back.final_url.endswith("/research/hub")
        forward = _observation(
            await research.step(session, HISTORY, {"tab": "t1", "direction": "forward"})
        )
        assert forward.final_url is not None and forward.final_url.endswith("/research/project")
    finally:
        await research.close_session(session)


async def test_a_session_the_worker_never_opened_is_refused_not_created(
    research: Research,
) -> None:
    """Never an implicit "open a fresh one": the refs would resolve wrongly."""
    with pytest.raises(BrowserWorkerRejectedError) as refused:
        await research.navigate(uuid.uuid4(), f"{research.site.base_url}/research/hub")
    assert "unknown_session" in refused.value.reason
    assert research.site.hits() == {}


async def test_closing_a_session_is_idempotent_and_reported_honestly(
    research: Research,
) -> None:
    session = await research.open_session()
    assert await research.close_session(session) == "CLOSED"
    assert await research.close_session(session) == "NOT_FOUND"


async def test_a_research_step_needs_an_attempt_and_the_research_site_name(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        for overrides in ({"attempt_id": None}, {"action_id": None}, {"site": "public_web"}):
            with pytest.raises(BrowserWorkerRejectedError):
                await research.navigate(
                    session, f"{research.site.base_url}/research/hub", **overrides
                )
        assert research.site.hits() == {}
    finally:
        await research.close_session(session)


# ---- reading and semantic refs ----------------------------------------------------


async def test_a_page_is_read_into_bounded_blocks_and_refs_with_hosts_not_addresses(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        response = await research.navigate(
            session, f"{research.site.base_url}/research/project"
        )
        observation = _observation(response)
        assert observation.provenance == "untrusted_environment"
        assert PROJECT["contributors"] in _text(observation)
        assert PROJECT["purpose"] in _text(observation)
        assert [block.id for block in observation.blocks] == [
            f"b{index + 1}" for index in range(len(observation.blocks))
        ]
        assert observation.links, "the project page offers a link back to the directory"
        for link in observation.links:
            # The model-facing projection has a ref, a label and a host. There
            # is no field for an address, so a page cannot put one in front of
            # a planner even as a suggestion.
            assert set(link.model_dump()) == {"id", "text", "host"}
            assert link.host == "127.0.0.1"
        # The addresses live beside the observation, for the controller only.
        assert set(_targets(response)) == {link.id for link in observation.links}
    finally:
        await research.close_session(session)


async def test_a_link_ref_is_followed_without_the_planner_naming_an_address(
    research: Research,
) -> None:
    """The multi-hop fixture: the hub holds no facts, a link leads to them."""
    session = await research.open_session()
    try:
        hub = await research.navigate(session, f"{research.site.base_url}/research/hub")
        observation = _observation(hub)
        targets = _targets(hub)
        ref = next(
            link.id
            for link in observation.links
            if targets[link.id].endswith("/research/project")
        )
        followed = _observation(
            await research.step(
                session,
                NAVIGATE,
                {
                    "tab": "t1",
                    "url": targets[ref],
                    "target_kind": "link",
                    "target_ref": ref,
                    "expected_document_epoch": observation.document_epoch,
                },
            )
        )
        assert PROJECT["contributors"] in _text(followed)
        assert followed.final_url is not None and followed.final_url.endswith("/research/project")
    finally:
        await research.close_session(session)


async def test_a_link_ref_from_a_document_the_tab_has_left_is_refused(
    research: Research,
) -> None:
    """The stale-reference fixture. `l5 of o3` means nothing at epoch 4."""
    session = await research.open_session()
    try:
        hub = await research.navigate(session, f"{research.site.base_url}/research/hub")
        observation = _observation(hub)
        targets = _targets(hub)
        ref = next(iter(targets))
        # The tab moves on. Every ref the previous document issued dies with it.
        await research.navigate(session, f"{research.site.base_url}/research/decoy/lamp")
        research.site.reset()
        stale = await research.step(
            session,
            NAVIGATE,
            {
                "tab": "t1",
                "url": targets[ref],
                "target_kind": "link",
                "target_ref": ref,
                "expected_document_epoch": observation.document_epoch,
            },
        )
        assert stale.status is OperationStatus.FAILED_BEFORE_EFFECT
        assert stale.error_code == "stale_document_epoch"
        assert research.site.hits() == {}
    finally:
        await research.close_session(session)


async def test_a_ref_the_worker_never_issued_is_refused(research: Research) -> None:
    session = await research.open_session()
    try:
        hub = await research.navigate(session, f"{research.site.base_url}/research/hub")
        observation = _observation(hub)
        refused = await research.step(
            session,
            NAVIGATE,
            {
                "tab": "t1",
                "url": f"{research.site.base_url}/research/project",
                "target_kind": "link",
                "target_ref": "l9",
                "expected_document_epoch": observation.document_epoch,
            },
        )
        assert refused.error_code == "unknown_target_ref"
    finally:
        await research.close_session(session)


async def test_a_ref_whose_address_disagrees_with_the_controller_is_refused(
    research: Research,
) -> None:
    """Two independent tables must agree. Disagreement is never reconciled."""
    session = await research.open_session()
    try:
        hub = await research.navigate(session, f"{research.site.base_url}/research/hub")
        observation = _observation(hub)
        ref = next(iter(_targets(hub)))
        research.site.reset()
        refused = await research.step(
            session,
            NAVIGATE,
            {
                "tab": "t1",
                "url": f"{research.site.base_url}/research/decoy/grinder",
                "target_kind": "link",
                "target_ref": ref,
                "expected_document_epoch": observation.document_epoch,
            },
        )
        assert refused.error_code == "target_mismatch"
        assert research.site.hits() == {}
    finally:
        await research.close_session(session)


async def test_links_are_bounded(research: Research) -> None:
    session = await research.open_session()
    try:
        observation = _observation(
            await research.navigate(session, f"{research.site.base_url}/research/loop/1")
        )
        assert len(observation.links) <= MAX_LINKS
        assert observation.total_link_count >= len(observation.links)
    finally:
        await research.close_session(session)


async def test_observing_a_tab_with_no_document_says_so_rather_than_inventing_one(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        observation = _observation(await research.step(session, OBSERVE, {"tab": "t1"}))
        assert observation.kind == "tab_state"
        assert observation.final_url is None
        assert observation.open_tabs == ["t1"]
    finally:
        await research.close_session(session)


async def test_observing_re_reads_the_tab_without_a_new_request(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        await research.navigate(session, f"{research.site.base_url}/research/project")
        research.site.reset()
        observation = _observation(await research.step(session, OBSERVE, {"tab": "t1"}))
        assert observation.kind == "page"
        assert PROJECT["contributors"] in _text(observation)
        # Re-reading the document Chromium already holds asks the site nothing.
        assert research.site.hits() == {}
    finally:
        await research.close_session(session)


async def test_scrolling_is_one_key_press_and_returns_a_fresh_observation(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        await research.navigate(session, f"{research.site.base_url}/research/project")
        observation = _observation(
            await research.step(session, SCROLL, {"tab": "t1", "direction": "down"})
        )
        assert observation.operation.value == "scroll"
        assert PROJECT["contributors"] in _text(observation)
    finally:
        await research.close_session(session)


# ---- tabs --------------------------------------------------------------------------


async def test_tabs_are_task_owned_bounded_and_never_the_last_one(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        opened = _observation(await research.step(session, TAB, {"action": "open"}))
        assert opened.open_tabs == ["t1", "t2"]
        second = _observation(await research.step(session, TAB, {"action": "open"}))
        assert second.open_tabs == ["t1", "t2", "t3"]
        # The worker's tab budget is three in this fixture.
        exhausted = await research.step(session, TAB, {"action": "open"})
        assert exhausted.error_code == "tab_budget_exhausted"

        await research.navigate(session, f"{research.site.base_url}/research/project", tab="t3")
        activated = _observation(await research.step(session, TAB, {"action": "activate", "tab": "t3"}))
        assert activated.tab == "t3"
        assert activated.final_url is not None and activated.final_url.endswith("/research/project")

        closed = _observation(await research.step(session, TAB, {"action": "close", "tab": "t3"}))
        assert closed.open_tabs == ["t1", "t2"]
        assert (await research.step(session, TAB, {"action": "activate", "tab": "t3"})).error_code == (
            "unknown_tab"
        )
        await research.step(session, TAB, {"action": "close", "tab": "t2"})
        last = await research.step(session, TAB, {"action": "close", "tab": "t1"})
        assert last.error_code == "last_tab"
    finally:
        await research.close_session(session)


# ---- the network boundary -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("https://127.0.0.1/x", "ip_literal"),
        ("https://localhost/x", "local_host"),
        ("https://10.0.0.5/x", "ip_literal"),
        ("https://192.168.1.1/x", "ip_literal"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal"),
        ("https://[::1]/x", "invalid_host"),
        ("https://metadata.google.internal/x", "local_host"),
        ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
        ("about:blank", "scheme_not_allowed"),
        ("chrome://settings", "scheme_not_allowed"),
        ("devtools://devtools/x", "scheme_not_allowed"),
        ("ws://127.0.0.1/socket", "scheme_not_allowed"),
        ("lumi://open", "scheme_not_allowed"),
        ("https://user:pass@example.com/", "credentials_in_url"),
    ],
)
async def test_the_worker_applies_its_own_destination_policy(
    research: Research, path: str, code: str
) -> None:
    session = await research.open_session()
    try:
        refused = await research.navigate(session, path)
        assert refused.status is OperationStatus.FAILED_BEFORE_EFFECT
        assert refused.error_code == code
    finally:
        await research.close_session(session)


async def test_a_host_outside_the_configured_research_scope_is_never_contacted(
    research: Research,
) -> None:
    """This worker's research policy names one test origin and nothing else."""
    session = await research.open_session()
    try:
        refused = await research.navigate(session, f"{research.canary.base_url}/secret")
        assert refused.error_code in ("https_required", "destination_not_allowed")
        assert research.canary.hits() == {}
    finally:
        await research.close_session(session)


async def test_hostile_page_text_is_only_data_and_every_channel_stays_closed(
    research: Research,
) -> None:
    """The hostile-prompt fixture. It has no authority over anything."""
    session = await research.open_session()
    try:
        observation = _observation(
            await research.navigate(
                session,
                f"{research.site.base_url}/research/hostile?canary={research.canary.base_url}",
            )
        )
        # The injection is quotable evidence, and nothing else.
        assert RESEARCH_INJECTION[:40] in _text(observation)
        assert observation.provenance == "untrusted_environment"
        # Nothing it asked for happened: no beacon, fetch, sendBeacon,
        # WebSocket or popup reached the canary, and no POST reached the site.
        assert research.canary.hits() == {}
        hits = research.site.hits()
        assert not [key for key in hits if "mutation-canary" in key]
        assert observation.open_tabs == ["t1"], "a popup never becomes a task-owned tab"
        # A link the page offers to a refused destination is not even a ref.
        assert all(link.host == "127.0.0.1" for link in observation.links)
    finally:
        await research.close_session(session)


async def test_an_allowed_redirect_is_followed_one_checked_hop_at_a_time(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        observation = _observation(
            await research.navigate(session, f"{research.site.base_url}/redirect/allowed")
        )
        assert observation.redirects and observation.final_url is not None
        assert observation.final_url.endswith("/profiles/rated")
        assert observation.requested_url is not None
        assert observation.requested_url.endswith("/redirect/allowed")
    finally:
        await research.close_session(session)


async def test_a_redirect_to_a_refused_destination_is_never_requested(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        refused = await research.navigate(
            session,
            f"{research.site.base_url}/redirect/to?target={research.canary.base_url}/stolen",
        )
        assert refused.status is OperationStatus.FAILED_BEFORE_EFFECT
        assert refused.error_code == "redirect_blocked"
        assert research.canary.hits() == {}
    finally:
        await research.close_session(session)


async def test_too_many_redirects_stop(research: Research) -> None:
    session = await research.open_session()
    try:
        refused = await research.navigate(session, f"{research.site.base_url}/redirect/chain/9")
        assert refused.error_code == "too_many_redirects"
    finally:
        await research.close_session(session)


@pytest.mark.parametrize(
    ("path", "code"),
    [("/download", "download_blocked"), ("/binary", "unsupported_content_type")],
)
async def test_downloads_and_non_documents_are_refused(
    research: Research, path: str, code: str
) -> None:
    session = await research.open_session()
    try:
        refused = await research.navigate(session, f"{research.site.base_url}{path}")
        assert refused.error_code == code
    finally:
        await research.close_session(session)


async def test_a_page_that_navigates_itself_away_is_refused_without_contacting_it(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        refused = await research.navigate(
            session, f"{research.site.base_url}/profiles/escape?canary={research.canary.base_url}"
        )
        assert refused.error_code == "navigation_blocked"
        assert research.canary.hits() == {}
    finally:
        await research.close_session(session)


@pytest.mark.parametrize("methods", ["POST", "PUT", "PATCH", "DELETE"])
async def test_same_origin_mutation_requests_never_leave_the_browser(
    research: Research, methods: str
) -> None:
    session = await research.open_session()
    try:
        _observation(
            await research.navigate(
                session, f"{research.site.base_url}/profiles/mutating?methods={methods}"
            )
        )
        hits = research.site.hits()
        assert not [key for key in hits if "mutation-canary" in key], hits
    finally:
        await research.close_session(session)


async def test_an_error_page_is_a_known_failure_not_an_observation(
    research: Research,
) -> None:
    session = await research.open_session()
    try:
        refused = await research.navigate(session, f"{research.site.base_url}/missing")
        assert refused.status is OperationStatus.FAILED_BEFORE_EFFECT
        assert refused.error_code == "page_http_error"
        assert refused.observation.get("http_status") == 404
    finally:
        await research.close_session(session)


async def test_the_decoy_pages_use_the_same_labels_with_different_numbers(
    research: Research,
) -> None:
    """Grounding, not label matching, is what has to pick the right page."""
    session = await research.open_session()
    try:
        decoy = _observation(
            await research.navigate(session, f"{research.site.base_url}/research/decoy/lamp")
        )
        assert DECOYS["lamp"]["contributors"] in _text(decoy)
        assert PROJECT["contributors"] not in _text(decoy).replace(
            DECOYS["lamp"]["contributors"], ""
        )
    finally:
        await research.close_session(session)


async def test_the_operations_accept_nothing_but_their_own_fields(
    research: Research,
) -> None:
    """No selector, script, method, header or coordinate reaches the worker."""
    session = await research.open_session()
    try:
        for operation, payload in (
            (NAVIGATE, {"tab": "t1", "url": f"{research.site.base_url}/x", "target_kind": "seed", "selector": "a"}),
            (OBSERVE, {"tab": "t1", "script": "window.x"}),
            (OBSERVE, {"tab": "t1", "xpath": "//a"}),
            (SCROLL, {"tab": "t1", "direction": "down", "x": 4}),
            (NAVIGATE, {"tab": "t1", "url": f"{research.site.base_url}/x", "target_kind": "seed", "method": "POST"}),
            (TAB, {"action": "open", "extra": 1}),
            (HISTORY, {"tab": "t1", "direction": "sideways"}),
        ):
            with pytest.raises(BrowserWorkerRejectedError) as refused:
                await research.step(session, operation, payload)
            assert "invalid_operation_input" in refused.value.reason
        assert research.site.hits() == {}
    finally:
        await research.close_session(session)
