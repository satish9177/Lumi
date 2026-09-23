"""Milestone 10 S2: controlled downloads and placement against real PostgreSQL and a real NTFS folder.

The browser worker is replaced by a fake that follows the worker's own quarantine protocol exactly
(`app.files.quarantine.begin` -> `finish`), so every crash point can be staged precisely: before the
request, after the request, mid-write, after completion. The real worker is covered by the browser suite.
"""

import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import ActionStatus, AttemptOutcome
from app.domain.browser_dispatch import DispatchStatus
from app.domain.public_url import PublicUrlPolicy, parse_test_origins
from app.domain.transfers import TransferRefusal
from app.files import quarantine
from app.services.actions import ActionService
from app.services.browser_execution import Outcome
from app.services.documents import DocumentService
from app.services.recovery import RecoveryService
from app.services.runtime import RuntimeGeneration
from app.services.transfers import TransferService
from tests.document_fixtures import make_docx, make_pdf

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the M10 file broker ships on Windows")

ORIGIN = "http://127.0.0.1:8899"
URL = f"{ORIGIN}/files/resume.pdf"


class FakeBrowser:
    """The worker's quarantine protocol, with a switch for every way a download can end."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.dispatches = 0
        self.body: bytes = make_pdf()
        self.mode: str = "ok"
        self.after: Callable[[], None] | None = None

    async def open_download_worker(self) -> tuple[Any, uuid.UUID]:
        class Client:
            async def aclose(self) -> None:
                return None

        return Client(), uuid.uuid4()

    async def run_download(self, client: Any, worker_generation: uuid.UUID, *, action_id: uuid.UUID, attempt_id: uuid.UUID, url: str, transfer_id: uuid.UUID, max_bytes: int) -> Outcome:
        self.dispatches += 1
        if self.mode == "never_reached":
            # The dispatch never reached the worker: no marker, no request.
            return Outcome(outcome=AttemptOutcome.OUTCOME_UNKNOWN, dispatch_status=DispatchStatus.OUTCOME_UNKNOWN, submitted=False, error_code="lost", observation_id=None, result={})
        directory = quarantine.begin(self.root, transfer_id, source_digest="0" * 64)
        if self.mode == "partial":
            (directory / quarantine.PARTIAL).write_bytes(self.body[:10])
            return Outcome(outcome=AttemptOutcome.OUTCOME_UNKNOWN, dispatch_status=DispatchStatus.OUTCOME_UNKNOWN, submitted=True, error_code="lost", observation_id=None, result={})
        if self.mode == "refused_type":
            return Outcome(outcome=AttemptOutcome.FAILED, dispatch_status=DispatchStatus.FAILED_BEFORE_EFFECT, submitted=True, error_code="download_type_refused", observation_id=None, result={})
        from app.documents.sniff import sniff

        manifest = quarantine.finish(directory, self.body, kind=sniff(self.body), content_type="application/pdf", host_url=ORIGIN + "/")
        observation = {"transfer_id": str(transfer_id), "final_url": url, "redirects": 0, "length": manifest.length, "sha256": manifest.sha256, "kind": manifest.kind, "content_type": "application/pdf"}
        if self.after is not None:
            self.after()
        if self.mode == "lost_after_effect":
            return Outcome(outcome=AttemptOutcome.OUTCOME_UNKNOWN, dispatch_status=DispatchStatus.OUTCOME_UNKNOWN, submitted=True, error_code="lost", observation_id=None, result={})
        if self.mode == "crash_after_effect":
            raise KeyboardInterrupt  # the runtime dies with the attempt EXECUTING
        return Outcome(outcome=AttemptOutcome.SUCCEEDED, dispatch_status=DispatchStatus.OK, submitted=True, error_code=None, observation_id=None, result={}, observation=observation)


@pytest.fixture
def folders(tmp_path: Path) -> dict[str, Path]:
    dest = tmp_path / "Downloads Approved"
    dest.mkdir()
    return {"dest": dest, "quarantine": tmp_path / "quarantine"}


@pytest.fixture
def browser(folders: dict[str, Path]) -> FakeBrowser:
    return FakeBrowser(str(folders["quarantine"]))


@pytest.fixture
def documents(engine: AsyncEngine) -> DocumentService:
    return DocumentService(engine, grant_ttl_seconds=600, protected_folders=((), ()))


@pytest.fixture
def service(engine: AsyncEngine, action_service: ActionService, runtime_generation: RuntimeGeneration, browser: FakeBrowser, folders: dict[str, Path]) -> TransferService:
    return TransferService(
        engine,
        actions=action_service,
        browser=browser,  # type: ignore[arg-type]
        policy=PublicUrlPolicy(test_origins=parse_test_origins(ORIGIN)),
        quarantine_root=str(folders["quarantine"]),
        runtime_generation=runtime_generation.id,
        grant_ttl_seconds=600,
    )


async def _root(documents: DocumentService, folder: Path, *, can_create: bool = True) -> uuid.UUID:
    root = await documents.register_root(path=str(folder), label="Downloads", can_read=True, can_create=can_create, can_modify=False)
    return root.root_id


async def _approved(service: TransferService, root_id: uuid.UUID, *, url: str = URL, name: str = "resume.pdf") -> uuid.UUID:
    view = await service.create(url=url, root_id=root_id, file_name=name, intent="My synthetic resume")
    assert view.grant is not None and view.phase == "awaiting_approval"
    view = await service.confirm(view.task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)
    assert view.phase == "approved"
    return view.task_id


async def _code(awaitable: Any) -> str:
    with pytest.raises(TransferRefusal) as refused:
        await awaitable
    return refused.value.code


# ---- the happy path ---------------------------------------------------------------------------------


async def test_download_goes_to_quarantine_first_then_is_placed_atomically(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    assert view.phase == "quarantined" and view.download_status == "SUCCEEDED"
    assert not (folders["dest"] / "resume.pdf").exists(), "nothing reaches the folder before placement"
    view = await service.place(task)
    assert view.phase == "placed" and view.place_status == "SUCCEEDED"
    placed = folders["dest"] / "resume.pdf"
    assert placed.read_bytes() == browser.body
    assert quarantine.has_zone_identifier(str(placed)), "Mark-of-the-Web is preserved, never stripped"
    assert view.grant is not None and view.grant.status.value == "COMPLETED"


async def test_a_repeated_download_request_never_fetches_twice(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)
    await service.download(task)
    assert browser.dispatches == 1


# ---- refusals before anything is spent ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("tool.exe", "destination_type_refused"),
        ("run.ps1", "destination_type_refused"),
        ("macro.docm", "destination_type_refused"),
        ("link.lnk", "destination_type_refused"),
        ("CON.pdf", "reserved_name"),
        ("..\\escape.pdf", "not_a_file_name"),
        ("resume.pdf:ads", "alternate_stream_or_drive"),
        ("resume.pdf.", "trailing_dot_or_space"),
    ],
)
async def test_an_unsafe_destination_name_is_refused(service: TransferService, documents: DocumentService, folders: dict[str, Path], name: str, code: str) -> None:
    root_id = await _root(documents, folders["dest"])
    assert await _code(service.create(url=URL, root_id=root_id, file_name=name, intent="x")) == code


async def test_read_permission_does_not_imply_create(service: TransferService, documents: DocumentService, folders: dict[str, Path]) -> None:
    root_id = await _root(documents, folders["dest"], can_create=False)
    assert await _code(service.create(url=URL, root_id=root_id, file_name="resume.pdf", intent="x")) == "permission_missing"


async def test_an_existing_destination_is_never_overwritten(service: TransferService, documents: DocumentService, folders: dict[str, Path]) -> None:
    (folders["dest"] / "Resume.PDF").write_bytes(b"the person's own file")
    root_id = await _root(documents, folders["dest"])
    assert await _code(service.create(url=URL, root_id=root_id, file_name="resume.pdf", intent="x")) == "destination_exists"


async def test_a_url_outside_the_download_policy_is_refused(service: TransferService, documents: DocumentService, folders: dict[str, Path]) -> None:
    root_id = await _root(documents, folders["dest"])
    for url in ("http://127.0.0.1:9999/x.pdf", "file:///C:/Windows/win.ini", "http://169.254.169.254/latest", "https://attacker.example/r.pdf"):
        with pytest.raises(TransferRefusal):
            await service.create(url=url, root_id=root_id, file_name="resume.pdf", intent="x")


async def test_nothing_runs_without_the_trusted_click(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    view = await service.create(url=URL, root_id=await _root(documents, folders["dest"]), file_name="resume.pdf", intent="x")
    assert await _code(service.download(view.task_id)) == "grant_not_active"
    assert browser.dispatches == 0


# ---- type safety ---------------------------------------------------------------------------------------


async def test_a_document_of_the_wrong_kind_is_a_known_download_that_is_never_placed(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    browser.body = make_docx()  # a DOCX served for resume.pdf
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    assert view.download_status == "SUCCEEDED" and view.transfer.status == "FAILED" and view.transfer.error_code == "type_mismatch"
    assert await _code(service.place(task)) == "wrong_phase"
    assert list(folders["dest"].iterdir()) == []


async def test_a_worker_refused_executable_leaves_nothing_to_place(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    browser.mode = "refused_type"
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    assert view.transfer.status == "FAILED"
    assert await _code(service.place(task)) == "wrong_phase"


async def test_a_quarantined_file_swapped_for_an_executable_is_refused_before_placement(service: TransferService, documents: DocumentService, folders: dict[str, Path]) -> None:
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    payload = quarantine.transfer_directory(service.quarantine_root, view.transfer.id) / quarantine.PAYLOAD
    payload.write_bytes(b"MZ" + b"\x00" * 100)
    assert await _code(service.place(task)) == "quarantine_changed"
    assert list(folders["dest"].iterdir()) == []


async def test_stripped_provenance_is_refused(service: TransferService, documents: DocumentService, folders: dict[str, Path]) -> None:
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    payload = quarantine.transfer_directory(service.quarantine_root, view.transfer.id) / quarantine.PAYLOAD
    os.remove(f"{payload}:{quarantine.ZONE_STREAM}")
    assert await _code(service.place(task)) == "provenance_missing"


# ---- races at the destination ----------------------------------------------------------------------------


async def test_a_file_that_appears_at_the_destination_before_the_rename_is_never_overwritten(service: TransferService, documents: DocumentService, folders: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.transfers as transfers_module

    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)
    real_place = transfers_module.place  # type: ignore[attr-defined]

    def racing_place(**kwargs: Any) -> Any:
        (folders["dest"] / "resume.pdf").write_bytes(b"appeared in the race window")
        return real_place(**kwargs)

    monkeypatch.setattr(transfers_module, "place", racing_place)
    view = await service.place(task)
    assert view.place_status == "FAILED" and view.transfer.error_code == "destination_exists"
    assert (folders["dest"] / "resume.pdf").read_bytes() == b"appeared in the race window"


async def test_a_destination_folder_replaced_after_approval_is_refused(service: TransferService, documents: DocumentService, folders: dict[str, Path]) -> None:
    import shutil

    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)
    shutil.rmtree(folders["dest"])
    folders["dest"].mkdir()
    assert await _code(service.place(task)) == "root_changed"


# ---- crashes and lost answers: evidence, never a second fetch --------------------------------------------------


async def test_a_lost_answer_after_the_download_is_reconciled_from_the_quarantine(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    browser.mode = "lost_after_effect"
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    assert view.phase == "download_unknown"
    view = await service.reconcile(task)
    assert view.download_status == "SUCCEEDED" and view.phase == "quarantined"
    view = await service.place(task)
    assert view.phase == "placed" and browser.dispatches == 1


async def test_a_dispatch_that_never_reached_the_worker_is_authoritatively_no_effect(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    browser.mode = "never_reached"
    root_id = await _root(documents, folders["dest"])
    task = await _approved(service, root_id)
    await service.download(task)
    view = await service.reconcile(task)
    assert view.download_status == "FAILED" and view.transfer.status == "FAILED"
    # The lock is released only because absence was authoritative; a new approval may fetch again.
    browser.mode = "ok"
    second = await _approved(service, root_id)
    assert (await service.download(second)).phase == "quarantined"


async def test_a_partial_transfer_stays_unknown_and_locks_the_same_effect(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    browser.mode = "partial"
    root_id = await _root(documents, folders["dest"])
    task = await _approved(service, root_id)
    await service.download(task)
    view = await service.reconcile(task)
    assert view.phase == "download_unknown"
    # Same URL, other name; other URL, same name; both are the same material effect while unresolved.
    for url, name in ((URL, "other.pdf"), (f"{ORIGIN}/files/other.pdf", "resume.pdf")):
        again = await _approved(service, root_id, url=url, name=name)
        assert await _code(service.download(again)) == "effect_locked"
    unrelated = await _approved(service, root_id, url=f"{ORIGIN}/files/unrelated.pdf", name="unrelated.pdf")
    browser.mode = "ok"
    assert (await service.download(unrelated)).phase == "quarantined"
    assert browser.dispatches == 2


async def test_a_runtime_that_died_mid_download_leaves_evidence_and_is_never_refetched(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path], engine: AsyncEngine) -> None:
    browser.mode = "crash_after_effect"
    task = await _approved(service, await _root(documents, folders["dest"]))
    with pytest.raises(KeyboardInterrupt):
        await service.download(task)
    await RecoveryService(engine).recover_unfinished_attempts(uuid.uuid4())
    view = await service.describe(task)
    assert view.download_status == "OUTCOME_UNKNOWN"
    browser.mode = "ok"
    await service.download(task)
    assert browser.dispatches == 1, "a download is never repeated because nothing was placed"
    view = await service.reconcile(task)
    assert view.phase == "quarantined"


async def test_a_crash_after_the_rename_but_before_the_commit_is_reconciled_as_placed(service: TransferService, documents: DocumentService, folders: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.transfers as transfers_module
    from app.files.place import PlacementRefusal

    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)
    real_place = transfers_module.place  # type: ignore[attr-defined]

    def lost_answer(**kwargs: Any) -> Any:
        real_place(**kwargs)
        raise PlacementRefusal("placement_unverified", effect_possible=True)

    monkeypatch.setattr(transfers_module, "place", lost_answer)
    view = await service.place(task)
    assert view.phase == "placement_unknown"
    view = await service.reconcile(task)
    assert view.place_status == "SUCCEEDED" and view.transfer.status == "PLACED"


async def test_a_placement_that_did_not_happen_is_authoritatively_not_placed(service: TransferService, documents: DocumentService, folders: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.transfers as transfers_module
    from app.files.place import PlacementRefusal

    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)

    def nothing(**_: Any) -> Any:
        raise PlacementRefusal("rename_failed", effect_possible=True)

    monkeypatch.setattr(transfers_module, "place", nothing)
    await service.place(task)
    view = await service.reconcile(task)
    # Authoritatively not placed: the transfer ends (its one placement step is spent) and becomes sweepable.
    assert view.place_status == "FAILED" and view.transfer.status == "FAILED" and view.transfer.error_code == "not_placed"


async def test_a_quarantined_payload_changed_before_reconciliation_stays_unknown(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path]) -> None:
    browser.mode = "lost_after_effect"
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    payload = quarantine.transfer_directory(service.quarantine_root, view.transfer.id) / quarantine.PAYLOAD
    payload.write_bytes(b"%PDF-1.4 tampered")
    view = await service.reconcile(task)
    assert view.phase == "download_unknown", "a hash mismatch is not evidence of a completed download"


# ---- isolation and lifetime ----------------------------------------------------------------------------------


async def test_another_task_cannot_drive_this_transfer(service: TransferService, documents: DocumentService, engine: AsyncEngine, folders: dict[str, Path]) -> None:
    from app.domain.errors import TaskKindMismatchError

    task = await _approved(service, await _root(documents, folders["dest"]))
    other = await documents.create_task()
    with pytest.raises(TaskKindMismatchError):
        await service.download(other.task_id)
    async with engine.connect() as connection:
        kinds = (await connection.execute(text("SELECT kind FROM task_grants WHERE task_id = :t"), {"t": task})).scalars().all()
    assert kinds == ["file_transfer"]


async def test_placed_quarantines_are_swept_and_unknown_ones_are_kept_as_evidence(service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path], engine: AsyncEngine) -> None:
    root_id = await _root(documents, folders["dest"])
    placed = await _approved(service, root_id)
    await service.download(placed)
    placed_view = await service.place(placed)
    browser.mode = "partial"
    unknown = await _approved(service, root_id, url=f"{ORIGIN}/files/two.pdf", name="two.pdf")
    unknown_view = await service.download(unknown)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE file_transfers SET updated_at = now() - interval '2 days'"))
    assert await service.sweep_quarantine() == 1
    assert not quarantine.transfer_directory(service.quarantine_root, placed_view.transfer.id).exists()
    assert quarantine.transfer_directory(service.quarantine_root, unknown_view.transfer.id).exists()
    assert (folders["dest"] / "resume.pdf").exists(), "the sweep never touches the approved folder"
    assert ActionStatus.OUTCOME_UNKNOWN.value == (await service.describe(unknown)).download_status


# ---- S2 adversarial review regressions ------------------------------------------------------------------------


async def test_a_crash_between_the_attempt_commit_and_the_row_update_is_still_reconcilable(
    service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path], engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 1: the action is found by its key, so its effect keys can never be locked for good."""
    from app.repositories.transfers import TransferRepository

    root_id = await _root(documents, folders["dest"])
    task = await _approved(service, root_id)
    real_update = TransferRepository.update

    async def dying_update(self: TransferRepository, transfer_id: uuid.UUID, **values: Any) -> Any:
        if "download_action_id" in values:
            raise KeyboardInterrupt  # the runtime dies right after the attempt committed
        return await real_update(self, transfer_id, **values)

    monkeypatch.setattr(TransferRepository, "update", dying_update)
    with pytest.raises(KeyboardInterrupt):
        await service.download(task)
    monkeypatch.setattr(TransferRepository, "update", real_update)
    await RecoveryService(engine).recover_unfinished_attempts(uuid.uuid4())
    assert (await service.describe(task)).download_status == "OUTCOME_UNKNOWN"
    assert browser.dispatches == 0
    # The same effect is locked until then...
    other = await _approved(service, root_id, name="other.pdf")
    assert await _code(service.download(other)) == "effect_locked"
    # ...and reconciliation finds the action and proves (with a tombstone) that no request was made.
    view = await service.reconcile(task)
    assert view.download_status == "FAILED" and view.transfer.status == "FAILED"
    assert (await service.download(other)).phase == "quarantined"


async def test_a_dispatch_arriving_after_reconciliation_can_never_fetch(
    service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path],
) -> None:
    """Review finding 2: 'no request was made' is made true by a tombstone the worker's own begin cannot pass."""
    browser.mode = "never_reached"
    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)
    view = await service.reconcile(task)
    assert view.download_status == "FAILED"
    with pytest.raises(FileExistsError):
        quarantine.begin(service.quarantine_root, view.transfer.id, source_digest="0" * 64)
    # And the tombstone survives the sweep.
    await service.sweep_quarantine()
    assert quarantine.claim_absence(service.quarantine_root, view.transfer.id) is True


async def test_a_same_size_rewrite_after_the_check_is_caught_through_the_rename_handle(
    service: TransferService, documents: DocumentService, folders: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding 4: the bytes are verified through the handle that renames, not by an earlier path read."""
    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    payload = quarantine.transfer_directory(service.quarantine_root, view.transfer.id) / quarantine.PAYLOAD
    real_verify = TransferService._verify_quarantined
    calls = 0

    def verify_then_tamper(self: TransferService, transfer: Any) -> None:
        nonlocal calls
        real_verify(self, transfer)
        calls += 1
        if calls == 2:  # the re-check just before the rename; the file keeps its index and size
            with open(payload, "r+b") as handle:
                handle.seek(10)
                handle.write(b"X")

    monkeypatch.setattr(TransferService, "_verify_quarantined", verify_then_tamper)
    view = await service.place(task)
    assert view.place_status == "FAILED" and view.transfer.error_code == "quarantine_changed"
    assert list(folders["dest"].iterdir()) == []


async def test_the_sweep_never_follows_a_junction_out_of_the_quarantine(
    service: TransferService, documents: DocumentService, folders: dict[str, Path], engine: AsyncEngine, tmp_path: Path,
) -> None:
    """Review finding 5."""
    import _winapi
    import shutil

    task = await _approved(service, await _root(documents, folders["dest"]))
    view = await service.download(task)
    await service.place(task)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / quarantine.PAYLOAD).write_bytes(b"someone else's file")
    directory = quarantine.transfer_directory(service.quarantine_root, view.transfer.id)
    shutil.rmtree(directory)
    _winapi.CreateJunction(str(victim), str(directory))
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE file_transfers SET updated_at = now() - interval '2 days'"))
    await service.sweep_quarantine()
    assert (victim / quarantine.PAYLOAD).read_bytes() == b"someone else's file"
    os.rmdir(directory)  # remove the junction itself


async def test_a_download_that_finishes_after_a_revoke_is_still_swept(
    service: TransferService, documents: DocumentService, browser: FakeBrowser, folders: dict[str, Path], engine: AsyncEngine,
) -> None:
    """Review finding 6: a quarantined payload whose grant can no longer place it does not linger."""
    import asyncio
    import threading

    from sqlalchemy.ext.asyncio import create_async_engine

    task = await _approved(service, await _root(documents, folders["dest"]))
    grant = (await service.describe(task)).grant
    assert grant is not None

    def revoke_mid_download() -> None:
        async def inner() -> None:
            other = create_async_engine(engine.url)
            try:
                async with other.begin() as connection:
                    await connection.execute(
                        text("UPDATE task_grants SET status = 'REVOKED', revoked_at = now() WHERE id = :g"), {"g": grant.id}
                    )
            finally:
                await other.dispose()

        worker = threading.Thread(target=lambda: asyncio.run(inner()))
        worker.start()
        worker.join()

    browser.after = revoke_mid_download
    view = await service.download(task)
    assert view.transfer.status == "QUARANTINED"
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE file_transfers SET updated_at = now() - interval '2 days'"))
    assert await service.sweep_quarantine() == 1
    assert not quarantine.transfer_directory(service.quarantine_root, view.transfer.id).exists()


async def test_the_generic_action_routes_can_never_touch_a_transfer_step(
    client: Any, service: TransferService, documents: DocumentService, folders: dict[str, Path], engine: AsyncEngine,
) -> None:
    """Review finding 8."""
    task = await _approved(service, await _root(documents, folders["dest"]))
    await service.download(task)
    async with engine.connect() as connection:
        action_id = (await connection.execute(text("SELECT id FROM actions WHERE task_id = :t"), {"t": task})).scalar_one()
    for path in ("attempts", "attempts/finish", "reconciliation", "reconciliation/finish"):
        response = await client.post(f"/actions/{action_id}/{path}", json={"outcome": "SUCCEEDED", "result": "SUCCEEDED"})
        assert response.status_code in (409, 422), (path, response.status_code)
    for tool in ("transfer_download", "transfer_place", "Transfer_Download"):
        planted = await client.post(
            f"/tasks/{task}/actions", json={"idempotency_key": "transfer-place", "tool_name": tool, "risk_tier": "R2", "proposal": {}}
        )
        assert planted.status_code in (409, 422), (tool, planted.status_code)
    async with engine.connect() as connection:
        count = (await connection.execute(text("SELECT count(*) FROM actions WHERE task_id = :t"), {"t": task})).scalar_one()
    assert count == 1
