"""The inspection network guard lets only GET and HEAD leave the browser context.

In-process: a fixture server on a thread, Chromium via Playwright, and the real
`PublicNetworkGuard` plus `inspect_public_page` handler, so the guard's own
refusal record can be inspected. A control run without the guard proves the
fixture really receives and counts mutation requests -- otherwise "nothing
arrived" would prove nothing.
"""

import asyncio
import socket
import threading
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import closing

import httpx
import pytest
import uvicorn
from playwright.async_api import Browser, async_playwright

from app.browser.network_guard import ALLOWED_METHODS, PublicNetworkGuard
from app.browser.operations.public_page import InspectPageInput, inspect_public_page
from app.browser.protocol import OperationStatus
from app.browser.registry import OperationContext
from app.domain.page_observation import PageObservation
from app.domain.public_url import PublicUrlPolicy
from evals.sites.public_pages import PROFILE, create_site

pytestmark = [pytest.mark.browser]

MUTATIONS = ("POST", "PUT", "PATCH", "DELETE")


def test_only_get_and_head_are_permitted() -> None:
    assert ALLOWED_METHODS == frozenset({"GET", "HEAD"})


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(create_site(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/__eval__/state", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def _hits(base: str) -> dict[str, int]:
    body: dict[str, dict[str, int]] = httpx.get(f"{base}/__eval__/state", timeout=10).json()
    return body["hits"]


def _reset(base: str) -> None:
    httpx.post(f"{base}/__eval__/reset", timeout=10).raise_for_status()


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch()
        try:
            yield chromium
        finally:
            await chromium.close()


async def test_control_without_the_guard_the_fixture_counts_every_mutation(site: str, browser: Browser) -> None:
    _reset(site)
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.goto(f"{site}/profiles/mutating")
        await page.get_by_text(PROFILE["contest_rating"]).wait_for(timeout=15_000)
    finally:
        await context.close()
    hits = _hits(site)
    for method in MUTATIONS:
        # One fetch and one XHR per method.
        assert hits.get(f"{method} /mutation-canary") == 2, hits


@pytest.mark.parametrize("methods", [*MUTATIONS, ",".join(MUTATIONS)])
async def test_the_guard_refuses_and_records_every_mutation_method(site: str, browser: Browser, methods: str) -> None:
    _reset(site)
    policy = PublicUrlPolicy(test_origins=frozenset({site}))
    context = await browser.new_context(service_workers="block", accept_downloads=False, permissions=[])
    try:
        page = await context.new_page()
        guard = PublicNetworkGuard(policy)
        await guard.install(context, page)
        result = await asyncio.wait_for(
            inspect_public_page(
                OperationContext(
                    page=page, origin="", dispatch_id=uuid.uuid4(), observation_id=uuid.uuid4(),
                    public_policy=policy, network_guard=guard,
                ),
                InspectPageInput(url=f"{site}/profiles/mutating?methods={methods.replace(',', '%2C')}"),
            ),
            timeout=60,
        )
    finally:
        await context.close()

    # 1. The page was inspected, and GET-rendered content is in the observation.
    assert result.status is OperationStatus.OK, (result.error_code, result.observation)
    observation = PageObservation.model_validate(result.observation)
    assert PROFILE["contest_rating"] in "\n".join(block.text for block in observation.blocks)
    # 2. No mutation request reached the fixture server.
    hits = _hits(site)
    assert not any(key.endswith("/mutation-canary") for key in hits), hits
    assert hits.get("/api/profile-stats") == 1
    # 3. The guard refused each attempt (fetch and XHR) and recorded the method.
    expected = Counter({method: 2 for method in methods.split(",")})
    assert guard.blocked_methods == expected
    assert guard.blocked["method_blocked"] == sum(expected.values())
    assert guard.main_frame_block is None
