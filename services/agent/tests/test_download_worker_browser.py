"""`download_to_quarantine` against a real worker, a real Chromium and the public-page fixture (M10 S2).

The fixture's request log is the evidence of how many times a URL was fetched; the quarantine directory
is the evidence of what was written. These tests talk to the worker directly with synthetic ids; the
ledger path is covered by test_transfers_service.py.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.browser.client import BrowserWorkerClient
from app.browser.protocol import DispatchRequest, DispatchResponse, OperationStatus
from app.domain.page_observation import PUBLIC_WEB_SITE
from app.domain.transfers import DOWNLOAD_TO_QUARANTINE
from app.files import quarantine
from evals.sites.public_pages.app import SYNTHETIC_PDF
from tests.browser_harness import PublicSiteControl, WorkerProcess, browser_worker, public_fixture_site

pytestmark = [pytest.mark.browser]


class Downloads:
    def __init__(self, site: PublicSiteControl, canary: PublicSiteControl, worker: WorkerProcess, root: Path) -> None:
        self.site = site
        self.canary = canary
        self.worker = worker
        self.root = root

    async def fetch(self, path_or_url: str, *, transfer_id: uuid.UUID | None = None, max_bytes: int = 1024 * 1024, **overrides: Any) -> tuple[uuid.UUID, DispatchResponse]:
        url = path_or_url if "://" in path_or_url else f"{self.site.base_url}{path_or_url}"
        transfer = transfer_id or uuid.uuid4()
        client = BrowserWorkerClient(base_url=self.worker.base_url, token=self.worker.token, runtime_generation=uuid.uuid4(), timeout_seconds=120)
        try:
            identity = await client.identify()
            request = DispatchRequest(**{
                "dispatch_id": uuid.uuid4(),
                "runtime_generation": client._runtime_generation,
                "expected_worker_generation": identity.worker_generation,
                "action_id": uuid.uuid4(),
                "attempt_id": uuid.uuid4(),
                "operation": DOWNLOAD_TO_QUARANTINE,
                "site": PUBLIC_WEB_SITE,
                "input": {"url": url, "transfer_id": str(transfer), "max_bytes": max_bytes},
                **overrides,
            })
            return transfer, await client.dispatch(request)
        finally:
            await client.aclose()

    def directory(self, transfer: uuid.UUID) -> Path:
        return quarantine.transfer_directory(str(self.root), transfer)


@pytest.fixture(scope="module")
def downloads(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Downloads]:
    logs: Path = tmp_path_factory.mktemp("downloads")
    root = logs / "quarantine"
    with public_fixture_site(logs / "site.log") as site, public_fixture_site(logs / "canary.log") as canary:
        with browser_worker(logs / "worker.log", site_origin="http://127.0.0.1:9", inspection_test_origins=site.base_url,
                            quarantine_root=str(root)) as worker:
            yield Downloads(PublicSiteControl(site.base_url), PublicSiteControl(canary.base_url), worker, root)


@pytest.fixture(autouse=True)
def _clean_logs(downloads: Downloads) -> None:
    downloads.site.reset()
    downloads.canary.reset()


async def test_an_approved_pdf_lands_in_quarantine_with_provenance_and_nowhere_else(downloads: Downloads) -> None:
    transfer, response = await downloads.fetch("/files/resume.pdf")
    assert response.status is OperationStatus.OK, (response.error_code, response.observation)
    assert response.submitted is True
    directory = downloads.directory(transfer)
    manifest = quarantine.read_complete(str(downloads.root), transfer)
    assert manifest is not None and manifest.kind == "pdf" and manifest.length == len(SYNTHETIC_PDF)
    assert (directory / quarantine.PAYLOAD).read_bytes() == SYNTHETIC_PDF
    assert quarantine.has_zone_identifier(str(directory / quarantine.PAYLOAD))
    assert sorted(p.name for p in directory.iterdir()) == sorted([quarantine.STARTED, quarantine.PAYLOAD, quarantine.COMPLETE])
    assert downloads.site.hits().get("/files/resume.pdf") == 1


@pytest.mark.parametrize("path", ["/files/tool.exe", "/files/disguised.pdf", "/files/script.pdf", "/profiles/rated"])
async def test_executables_scripts_and_pages_are_refused_by_their_bytes(downloads: Downloads, path: str) -> None:
    transfer, response = await downloads.fetch(path)
    assert response.status is OperationStatus.FAILED_BEFORE_EFFECT and response.error_code == "download_type_refused"
    directory = downloads.directory(transfer)
    assert not (directory / quarantine.PAYLOAD).exists() and not (directory / quarantine.PARTIAL).exists()
    assert quarantine.read_complete(str(downloads.root), transfer) is None


async def test_a_body_over_the_approved_limit_is_never_written(downloads: Downloads) -> None:
    transfer, response = await downloads.fetch("/files/large.pdf", max_bytes=4096)
    assert response.status is not OperationStatus.OK
    assert not (downloads.directory(transfer) / quarantine.PAYLOAD).exists()


async def test_a_redirect_is_followed_one_validated_hop_at_a_time(downloads: Downloads) -> None:
    transfer, response = await downloads.fetch("/files/redirect.pdf")
    assert response.status is OperationStatus.OK
    assert response.observation["redirects"] == 1 and response.observation["final_url"].endswith("/files/resume.pdf")


async def test_a_redirect_to_an_unapproved_origin_is_never_contacted(downloads: Downloads) -> None:
    target = f"{downloads.canary.base_url}/files/resume.pdf"
    transfer, response = await downloads.fetch(f"/redirect/to?target={target}")
    assert response.status is not OperationStatus.OK
    assert downloads.canary.hits() == {}
    assert not (downloads.directory(transfer) / quarantine.PAYLOAD).exists()


async def test_an_error_status_is_not_a_document(downloads: Downloads) -> None:
    transfer, response = await downloads.fetch("/files/gone.pdf")
    assert response.status is not OperationStatus.OK
    assert not (downloads.directory(transfer) / quarantine.PAYLOAD).exists()


async def test_a_transfer_id_fetches_at_most_once(downloads: Downloads) -> None:
    transfer, first = await downloads.fetch("/files/resume.pdf")
    assert first.status is OperationStatus.OK
    _, second = await downloads.fetch("/files/resume.pdf", transfer_id=transfer)
    assert second.status is OperationStatus.FAILED_BEFORE_EFFECT and second.error_code == "transfer_already_started"
    assert downloads.site.hits().get("/files/resume.pdf") == 1


async def test_a_url_outside_the_worker_policy_is_refused_before_any_marker(downloads: Downloads) -> None:
    transfer, response = await downloads.fetch(f"{downloads.canary.base_url}/files/resume.pdf")
    assert response.status is OperationStatus.FAILED_BEFORE_EFFECT
    assert not downloads.directory(transfer).exists()
    assert downloads.canary.hits() == {}
