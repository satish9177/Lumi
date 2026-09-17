"""`inspect_public_page` against the deterministic public-page fixture.

A real worker process, a real Chromium, and two fixture processes: the site the
worker is allowed to read, and a *canary* it is not. The canary's request log is
the evidence that a refused destination was never contacted -- not merely that
Lumi reported refusing it.

These tests talk to the worker directly with synthetic action and attempt ids.
The ledger path (approval, attempt, dispatch record, observation storage) is
covered in test_page_inspection_ledger.py.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.browser.client import BrowserWorkerClient
from app.browser.errors import BrowserWorkerRejectedError
from app.browser.protocol import DispatchRequest, DispatchResponse, OperationStatus
from app.domain.page_observation import (
    INSPECT_PUBLIC_PAGE,
    MAX_LINKS,
    PUBLIC_WEB_SITE,
    PageObservation,
)
from evals.sites.public_pages import HOSTILE_INSTRUCTIONS, PROFILE
from tests.browser_harness import (
    PublicSiteControl,
    WorkerProcess,
    browser_worker,
    public_fixture_site,
)

pytestmark = [pytest.mark.browser]


class Pages:
    def __init__(self, site: PublicSiteControl, canary: PublicSiteControl, worker: WorkerProcess) -> None:
        self.site = site
        self.canary = canary
        self.worker = worker

    async def inspect(self, url: str, **overrides: Any) -> DispatchResponse:
        client = BrowserWorkerClient(
            base_url=self.worker.base_url,
            token=self.worker.token,
            runtime_generation=uuid.uuid4(),
            timeout_seconds=120,
        )
        try:
            identity = await client.identify()
            request = DispatchRequest(
                **{
                    "dispatch_id": uuid.uuid4(),
                    "runtime_generation": client._runtime_generation,
                    "expected_worker_generation": identity.worker_generation,
                    "action_id": uuid.uuid4(),
                    "attempt_id": uuid.uuid4(),
                    "operation": INSPECT_PUBLIC_PAGE,
                    "site": PUBLIC_WEB_SITE,
                    "input": {"url": url},
                    **overrides,
                }
            )
            return await client.dispatch(request)
        finally:
            await client.aclose()


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pages]:
    logs: Path = tmp_path_factory.mktemp("public-pages")
    with public_fixture_site(logs / "site.log") as site, public_fixture_site(logs / "canary.log") as canary:
        with browser_worker(
            logs / "worker.log",
            site_origin="http://127.0.0.1:9",
            inspection_test_origins=site.base_url,
        ) as worker:
            yield Pages(PublicSiteControl(site.base_url), PublicSiteControl(canary.base_url), worker)


@pytest.fixture(autouse=True)
def _clean_logs(pages: Pages) -> None:
    pages.site.reset()
    pages.canary.reset()


def _observation(response: DispatchResponse) -> PageObservation:
    assert response.status is OperationStatus.OK, (response.error_code, response.observation)
    assert response.submitted is False
    return PageObservation.model_validate(response.observation)


def _text(observation: PageObservation) -> str:
    return "\n".join(block.text for block in observation.blocks)


async def test_a_profile_is_read_into_labelled_bounded_blocks(pages: Pages) -> None:
    url = f"{pages.site.base_url}/profiles/rated"
    observation = _observation(await pages.inspect(url))
    assert observation.requested_url == observation.final_url == url
    assert observation.provenance == "untrusted_environment"
    assert observation.document_epoch == 1 and observation.settled
    assert observation.title == f"{PROFILE['handle']} - Profile"
    texts = [block.text for block in observation.blocks]
    rating = texts.index("Contest rating")
    assert texts[rating + 1] == PROFILE["contest_rating"]
    assert texts[texts.index("Global rank") + 1] == PROFILE["global_rank"]
    assert texts[texts.index("Problems solved") + 1] == PROFILE["problems_solved"]
    assert {link.url for link in observation.links} >= {
        f"{pages.site.base_url}/problems",
        "https://github.com/",
    }
    assert observation.truncated is False
    assert observation.content_hash and len(observation.content_hash) == 64


async def test_a_profile_without_a_rating_has_no_rating_to_find(pages: Pages) -> None:
    observation = _observation(await pages.inspect(f"{pages.site.base_url}/profiles/unrated"))
    text = _text(observation)
    assert "Contest rating" not in text and PROFILE["contest_rating"] not in text
    assert PROFILE["global_rank"] in text and "No contest history yet." in text


async def test_hostile_text_is_only_data_and_every_exfiltration_channel_is_closed(pages: Pages) -> None:
    url = f"{pages.site.base_url}/profiles/hostile?canary={pages.canary.base_url}"
    observation = _observation(await pages.inspect(url))
    # The instructions are observed as text -- nothing more.
    assert HOSTILE_INSTRUCTIONS in _text(observation)
    assert PROFILE["contest_rating"] in _text(observation)
    # Beacon image, fetch, sendBeacon, WebSocket, popup: none reached the canary.
    assert pages.canary.hits() == {}
    # And the only page the site served was the one approved.
    assert pages.site.hits() == {"/profiles/hostile": 1}


async def test_a_page_that_navigates_itself_away_is_refused_without_contacting_it(pages: Pages) -> None:
    response = await pages.inspect(f"{pages.site.base_url}/profiles/escape?canary={pages.canary.base_url}")
    assert response.status is OperationStatus.FAILED_BEFORE_EFFECT
    assert response.error_code == "navigation_blocked"
    assert pages.canary.hits() == {}


async def test_late_rendered_content_is_waited_for(pages: Pages) -> None:
    observation = _observation(await pages.inspect(f"{pages.site.base_url}/profiles/dynamic"))
    assert observation.settled
    assert PROFILE["contest_rating"] in _text(observation)
    assert "Loading statistics" not in _text(observation)
    assert pages.site.hits().get("/api/profile-stats") == 1


async def test_a_page_that_replaces_itself_is_observed_at_its_new_document_epoch(pages: Pages) -> None:
    observation = _observation(await pages.inspect(f"{pages.site.base_url}/profiles/replacing"))
    assert observation.final_url == f"{pages.site.base_url}/profiles/rated?from=replacing"
    assert observation.document_epoch >= 2
    assert "This profile has moved." not in _text(observation)
    assert PROFILE["contest_rating"] in _text(observation)


async def test_text_that_never_settles_is_marked_unsettled_not_silently_trusted(pages: Pages) -> None:
    observation = _observation(await pages.inspect(f"{pages.site.base_url}/profiles/restless"))
    assert observation.settled is False
    assert observation.document_epoch == 1


async def test_an_allowed_redirect_chain_is_followed_one_checked_hop_at_a_time(pages: Pages) -> None:
    base = pages.site.base_url
    observation = _observation(await pages.inspect(f"{base}/redirect/chain/3"))
    assert observation.requested_url == f"{base}/redirect/chain/3"
    assert observation.redirects == [f"{base}/redirect/chain/2", f"{base}/redirect/chain/1", f"{base}/profiles/rated"]
    assert observation.final_url == f"{base}/profiles/rated"


async def test_too_many_redirects_stop(pages: Pages) -> None:
    response = await pages.inspect(f"{pages.site.base_url}/redirect/chain/9")
    assert (response.status, response.error_code) == (OperationStatus.FAILED_BEFORE_EFFECT, "too_many_redirects")


@pytest.mark.parametrize(
    ("target", "refusal"),
    [
        ("{canary}/profiles/rated", "https_required"),
        ("http://169.254.169.254/latest/meta-data/", "https_required"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal"),
        ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
        ("https://localhost/", "local_host"),
        ("https://evil.example.com/", "destination_not_allowed"),
    ],
)
async def test_a_forbidden_redirect_is_refused_before_it_is_requested(
    pages: Pages, target: str, refusal: str
) -> None:
    destination = target.format(canary=pages.canary.base_url)
    response = await pages.inspect(f"{pages.site.base_url}/redirect/to?target={destination}")
    assert response.status is OperationStatus.FAILED_BEFORE_EFFECT
    assert response.error_code == "redirect_blocked"
    assert response.observation.get("redirect_refusal") == refusal
    assert pages.canary.hits() == {}


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/download", "download_blocked"),
        ("/binary", "unsupported_content_type"),
    ],
)
async def test_downloads_and_non_documents_are_refused(pages: Pages, path: str, code: str) -> None:
    response = await pages.inspect(f"{pages.site.base_url}{path}")
    assert (response.status, response.error_code) == (OperationStatus.FAILED_BEFORE_EFFECT, code)


async def test_an_error_page_is_a_known_failure_not_an_observation(pages: Pages) -> None:
    response = await pages.inspect(f"{pages.site.base_url}/missing")
    assert response.status is OperationStatus.FAILED_BEFORE_EFFECT
    assert response.error_code == "page_http_error"
    assert response.observation.get("http_status") == 404


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("{canary}/profiles/rated", "https_required"),
        ("https://github.com/", "destination_not_allowed"),
        ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("https://user:pw@github.com/", "credentials_in_url"),
    ],
)
async def test_the_worker_applies_its_own_policy_to_the_approved_url(pages: Pages, url: str, code: str) -> None:
    response = await pages.inspect(url.format(canary=pages.canary.base_url))
    assert (response.status, response.error_code) == (OperationStatus.FAILED_BEFORE_EFFECT, code)
    assert pages.canary.hits() == {}


async def test_the_operation_accepts_nothing_but_a_url(pages: Pages) -> None:
    for extra in (
        {"selector": "#rating"},
        {"script": "document.body.innerText"},
        {"headers": {"Cookie": "a=b"}},
        {"browser_args": ["--no-sandbox"]},
        {"wait_for": "networkidle"},
    ):
        with pytest.raises(BrowserWorkerRejectedError) as refused:
            await pages.inspect(f"{pages.site.base_url}/profiles/rated", input={"url": "x", **extra})
        assert "invalid_operation_input" in str(refused.value)


async def test_a_public_page_operation_requires_an_attempt_and_the_public_site_name(pages: Pages) -> None:
    url = f"{pages.site.base_url}/profiles/rated"
    with pytest.raises(BrowserWorkerRejectedError) as no_attempt:
        await pages.inspect(url, attempt_id=None)
    assert "attempt_required" in str(no_attempt.value)
    with pytest.raises(BrowserWorkerRejectedError) as wrong_site:
        await pages.inspect(url, site="appointment_fixture")
    assert "site_not_allowed" in str(wrong_site.value)
    assert pages.site.hits() == {}


async def test_links_are_bounded(pages: Pages) -> None:
    observation = _observation(await pages.inspect(f"{pages.site.base_url}/profiles/rated"))
    assert len(observation.links) <= MAX_LINKS
    assert all(link.url.startswith(("http://", "https://")) for link in observation.links)
