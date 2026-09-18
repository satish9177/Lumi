"""Manual smoke test for Milestone 7b: read real public pages.

The automated suite proves the pipeline against a controlled fixture. This
script proves the *network* path: a real worker, real Chromium, the real
`public-research-v1` policy and the real public internet. It is opt-in because
it makes outbound requests, and it is a read-only script -- it opens a research
session, navigates to each address given, observes, and prints what came back.

    uv run python -m scripts.research_smoke https://github.com/satish9177/Lumi

There is no planner and no model here on purpose: the point is to see what the
public web actually returns, including a refusal. A site that blocks automated
browsers is reported as it answered, never worked around.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

from app.browser.client import BrowserWorkerClient
from app.browser.operations.research import NAVIGATE, OBSERVE
from app.browser.protocol import DispatchRequest, DispatchResponse, SessionRequest
from app.domain.research import PUBLIC_RESEARCH_SITE, ResearchObservation

# The process harness lives with the tests. It is test-support code, not app
# code, and a smoke script is exactly the other caller it was written for.
from tests.browser_harness import browser_worker

DEFAULT_URLS = (
    "https://github.com/satish9177/Lumi",
    "https://docs.python.org/3/library/asyncio-task.html",
    "https://leetcode.com/problems/two-sum/",
)


class Smoke:
    def __init__(self, base_url: str, token: Any) -> None:
        self.base_url = base_url
        self.token = token
        self.runtime_generation = uuid.uuid4()
        self.sequence = 0

    def _client(self) -> BrowserWorkerClient:
        return BrowserWorkerClient(
            base_url=self.base_url,
            token=self.token,
            runtime_generation=self.runtime_generation,
            timeout_seconds=120,
        )

    async def open_session(self) -> uuid.UUID:
        session_id = uuid.uuid4()
        client = self._client()
        try:
            identity = await client.identify()
            await client.open_session(
                SessionRequest(
                    session_id=session_id,
                    runtime_generation=self.runtime_generation,
                    expected_worker_generation=identity.worker_generation,
                )
            )
        finally:
            await client.aclose()
        return session_id

    async def step(
        self, session_id: uuid.UUID, operation: str, payload: dict[str, Any]
    ) -> DispatchResponse:
        self.sequence += 1
        client = self._client()
        try:
            identity = await client.identify()
            return await client.dispatch(
                DispatchRequest(
                    dispatch_id=uuid.uuid4(),
                    runtime_generation=self.runtime_generation,
                    expected_worker_generation=identity.worker_generation,
                    action_id=uuid.uuid4(),
                    attempt_id=uuid.uuid4(),
                    operation=operation,
                    site=PUBLIC_RESEARCH_SITE,
                    session_id=session_id,
                    input={"sequence": self.sequence, **payload},
                )
            )
        finally:
            await client.aclose()


def out(line: str) -> None:
    """Print without depending on the console code page.

    Page text is arbitrary Unicode and a Windows console is often cp1252. A
    smoke script that dies on an em dash has told you nothing.
    """
    encoding = sys.stdout.encoding or "ascii"
    sys.stdout.write(line.encode(encoding, "replace").decode(encoding) + "\n")


def report(url: str, response: DispatchResponse) -> None:
    out(f"\n=== {url}")
    out(f"  status      {response.status.value}")
    if response.error_code is not None:
        out(f"  refused     {response.error_code}")
        out(f"  detail      {response.observation}")
        return
    observation = ResearchObservation.model_validate(response.observation["observation"])
    text = "\n".join(block.text for block in observation.blocks)
    out(f"  final url   {observation.final_url}")
    out(f"  title       {observation.title}")
    out(f"  epoch       {observation.document_epoch}")
    out(f"  settled     {observation.settled}   truncated {observation.truncated}")
    out(f"  blocks      {len(observation.blocks)} ({len(text)} chars)")
    out(f"  links       {len(observation.links)} of {observation.total_link_count}")
    for block in observation.blocks[:6]:
        out(f"    [{block.id}] {block.text[:100]}")
    for link in observation.links[:5]:
        out(f"    [{link.id}] {link.text[:60]} -> {link.host}")


async def main(urls: tuple[str, ...]) -> int:
    logs = Path("research-smoke.log")
    out(f"worker log: {logs}")
    with browser_worker(
        logs,
        site_origin="http://127.0.0.1:9",
        research_any_public_host=True,
    ) as worker:
        smoke = Smoke(worker.base_url, worker.token)
        session_id = await smoke.open_session()
        out(f"session {session_id} open")
        for url in urls:
            response = await smoke.step(
                session_id, NAVIGATE, {"tab": "t1", "url": url, "target_kind": "seed"}
            )
            report(url, response)
        observed = await smoke.step(session_id, OBSERVE, {"tab": "t1"})
        report("(re-observe the current tab)", observed)
    return 0


if __name__ == "__main__":
    given = tuple(sys.argv[1:]) or DEFAULT_URLS
    raise SystemExit(asyncio.run(main(given)))
