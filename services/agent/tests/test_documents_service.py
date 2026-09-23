"""Milestone 10 S1: the document service against real PostgreSQL and a real directory tree."""

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.documents import DocumentRefusal
from app.repositories.desktop_disclosure import DesktopDisclosureRepository
from app.services.documents import DocumentService, DocumentTaskView
from tests.document_fixtures import make_docx, make_pdf, make_text

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the M10 file broker ships on Windows")


@pytest.fixture
def service(engine: AsyncEngine) -> DocumentService:
    # Roots live under the system temp folder (inside LocalAppData); the real protected set is tested in
    # test_file_broker.py.
    return DocumentService(engine, grant_ttl_seconds=600, protected_folders=((), ()))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "Approved Docs"
    (root / "Jobs").mkdir(parents=True)
    (root / "resume.pdf").write_bytes(make_pdf())
    (root / "Jobs" / "job.txt").write_bytes(make_text())
    (root / "cover.docx").write_bytes(make_docx())
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("outside the root", encoding="utf-8")
    return root


async def _code(awaitable: Any) -> str:
    with pytest.raises(DocumentRefusal) as refused:
        await awaitable
    return refused.value.code


async def _two_documents(service: DocumentService, folder: Path) -> DocumentTaskView:
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task(objective="compare my resume to a job")
    view = await service.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
    view = await service.add_root_file(task.task_id, root_id=root.root_id, relative_path="Jobs/job.txt")
    for item in view.files:
        view = await service.extract(task.task_id, file_id=item.file_id)
    assert len(view.documents) == 2
    return view


# ---- roots and permissions --------------------------------------------------------------------------


async def test_a_root_is_registered_with_explicit_permissions_and_no_path_in_its_view(service: DocumentService, folder: Path) -> None:
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    assert (root.can_read, root.can_create, root.can_modify) == (True, False, False)
    assert str(folder) not in repr(root) and str(folder.resolve()) not in repr(root)
    listing = await service.list_root_files(root.root_id)
    assert sorted(item.relative_path for item in listing.files) == ["Jobs/job.txt", "cover.docx", "resume.pdf"]


async def test_modify_is_represented_but_refused_in_m10(service: DocumentService, folder: Path) -> None:
    assert await _code(service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=True)) == "modify_not_supported"
    assert await _code(service.register_root(path=str(folder), label="Docs", can_read=False, can_create=False, can_modify=False)) == "permission_missing"


async def test_one_live_root_per_folder(service: DocumentService, folder: Path) -> None:
    await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    assert await _code(service.register_root(path=str(folder), label="Again", can_read=True, can_create=True, can_modify=False)) == "root_already_approved"


async def test_create_permission_does_not_imply_read(service: DocumentService, folder: Path) -> None:
    root = await service.register_root(path=str(folder), label="Drop zone", can_read=False, can_create=True, can_modify=False)
    task = await service.create_task()
    assert await _code(service.list_root_files(root.root_id)) == "permission_missing"
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")) == "permission_missing"


async def test_a_revoked_root_stops_every_later_read(service: DocumentService, folder: Path) -> None:
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    view = await service.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
    await service.revoke_root(root.root_id, expected_revision=root.revision)
    assert await _code(service.extract(task.task_id, file_id=view.files[0].file_id)) == "root_revoked"
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path="cover.docx")) == "root_revoked"


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("../outside/secret.txt", "traversal"),
        ("..\\outside\\secret.txt", "traversal"),
        ("C:\\Windows\\win.ini", "absolute_path"),
        ("\\\\server\\share\\x.pdf", "device_or_unc_path"),
        ("resume.pdf:stream", "alternate_stream_or_drive"),
        ("CON.pdf", "reserved_name"),
        ("resume.pdf.", "trailing_dot_or_space"),
        ("missing.pdf", "file_missing"),
        ("tool.exe", "unsupported_format"),
    ],
)
async def test_a_name_that_escapes_or_confuses_is_refused(service: DocumentService, folder: Path, relative: str, code: str) -> None:
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path=relative)) == code


async def test_a_junction_inside_the_root_is_never_followed(service: DocumentService, folder: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(folder.parent / "outside"), str(folder / "escape"))
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path="escape/secret.txt")) == "reparse_point"


async def test_a_disguised_file_is_refused_by_its_bytes(service: DocumentService, folder: Path) -> None:
    (folder / "resume2.pdf").write_bytes(b"MZ\x90\x00 an executable pretending")
    (folder / "letter.pdf").write_bytes(make_docx())
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume2.pdf")) == "unsupported_format"
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path="letter.pdf")) == "type_mismatch"


# ---- dropped files -----------------------------------------------------------------------------------


async def test_a_dropped_file_authorises_that_file_and_nothing_around_it(service: DocumentService, folder: Path) -> None:
    task = await service.create_task()
    view = await service.add_dropped_file(task.task_id, path=str((folder / "resume.pdf").resolve()), display_name="resume.pdf")
    assert [item.source for item in view.files] == ["DROPPED_FILE"]
    assert await service.list_roots() == []  # no root, no folder, no siblings
    view = await service.extract(task.task_id, file_id=view.files[0].file_id)
    assert view.documents and view.documents[0].format == "pdf"
    assert await _code(service.add_root_file(task.task_id, root_id=uuid.uuid4(), relative_path="cover.docx")) == "root_not_found"


async def test_a_dropped_name_that_does_not_match_the_file_is_refused(service: DocumentService, folder: Path) -> None:
    task = await service.create_task()
    assert await _code(service.add_dropped_file(task.task_id, path=str((folder / "resume.pdf").resolve()), display_name="other.pdf")) == "path_mismatch"
    assert await _code(service.add_dropped_file(task.task_id, path=str(folder.resolve()), display_name="Approved Docs")) in ("not_a_regular_file", "unsupported_format")


# ---- identity -----------------------------------------------------------------------------------------


async def test_a_file_replaced_under_the_same_name_does_not_inherit_the_authority(service: DocumentService, folder: Path) -> None:
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    view = await service.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
    os.remove(folder / "resume.pdf")
    (folder / "resume.pdf").write_bytes(make_pdf([["A different person's resume"]]))
    assert await _code(service.extract(task.task_id, file_id=view.files[0].file_id)) == "file_changed"


async def test_extraction_is_bounded_and_refusals_are_recorded_without_text(service: DocumentService, folder: Path) -> None:
    (folder / "locked.pdf").write_bytes(make_pdf(encrypted=True))
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    view = await service.add_root_file(task.task_id, root_id=root.root_id, relative_path="locked.pdf")
    assert await _code(service.extract(task.task_id, file_id=view.files[0].file_id)) == "encrypted_pdf"


# ---- task isolation ---------------------------------------------------------------------------------------


async def test_another_task_cannot_use_this_tasks_file_document_or_approval(service: DocumentService, folder: Path) -> None:
    view = await _two_documents(service, folder)
    other = await service.create_task()
    assert await _code(service.extract(other.task_id, file_id=view.files[0].file_id)) == "file_not_found"
    first, second = (item.document_id for item in view.documents)
    assert await _code(service.compare_local(other.task_id, first_id=first, second_id=second)) == "document_not_found"
    assert await _code(
        service.create_disclosure(other.task_id, document_ids=[first], recipient="scripted", model="scripted-text", purpose="compare")
    ) == "document_not_found"
    card = await service.create_disclosure(view.task_id, document_ids=[first, second], recipient="scripted", model="scripted-text", purpose="compare")
    assert card.card is not None
    assert await _code(service.confirm(other.task_id, grant_id=card.card.grant_id, expected_revision=card.card.grant_revision)) == "grant_not_found"
    assert await _code(service.claim(other.task_id)) == "grant_not_found"


async def test_a_document_grant_is_not_a_desktop_grant(service: DocumentService, engine: AsyncEngine, folder: Path) -> None:
    view = await _two_documents(service, folder)
    card = await service.create_disclosure(
        view.task_id, document_ids=[view.documents[0].document_id], recipient="scripted", model="scripted-text", purpose="summarise"
    )
    assert card.card is not None
    async with engine.connect() as connection:
        assert await DesktopDisclosureRepository(connection).get_grant(card.card.grant_id) is None


# ---- local comparison -----------------------------------------------------------------------------------


async def test_local_comparison_is_deterministic_and_sends_nothing(service: DocumentService, folder: Path) -> None:
    view = await _two_documents(service, folder)
    first, second = (item.document_id for item in view.documents)
    comparison = await service.compare_local(view.task_id, first_id=first, second_id=second)
    assert "postgresql" in comparison.shared_terms and "python" in comparison.shared_terms
    assert comparison == await service.compare_local(view.task_id, first_id=first, second_id=second)
    assert await _code(service.compare_local(view.task_id, first_id=first, second_id=first)) == "same_document"


# ---- the exact disclosure ---------------------------------------------------------------------------------


def _grounded(excerpt_quote: str) -> dict[str, Any]:
    return {
        "kind": "comparison",
        "summary": "The resume covers the core requirements.",
        "findings": [{"kind": "match", "text": "Both mention PostgreSQL.", "evidence": [{"doc_ref": "d1", "quote": excerpt_quote}, {"doc_ref": "d2", "quote": "Python and PostgreSQL experience"}]}],
    }


async def _approved(service: DocumentService, folder: Path) -> DocumentTaskView:
    view = await _two_documents(service, folder)
    ids = [item.document_id for item in view.documents]
    view = await service.create_disclosure(view.task_id, document_ids=ids, recipient="scripted", model="scripted-text", purpose="How well does my resume match?")
    assert view.card is not None and view.phase == "awaiting_approval"
    return await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)


async def test_the_card_shows_exactly_the_redacted_excerpts_and_never_a_path(service: DocumentService, folder: Path) -> None:
    view = await _approved(service, folder)
    assert view.card is not None
    excerpts = " ".join(item.excerpt or "" for item in view.card.documents)
    assert "alex.example@example.test" not in excerpts and "\u27e6email:" in excerpts
    assert str(folder) not in repr(view.card)
    context = await service.claim(view.task_id)
    rendered = repr(context.projection)
    assert "resume.pdf" not in rendered and "job.txt" not in rendered and str(folder.parent) not in rendered
    assert [item["doc_ref"] for item in context.projection["documents"]] == ["d1", "d2"]


async def test_one_approval_funds_exactly_one_provider_call(service: DocumentService, folder: Path) -> None:
    view = await _approved(service, folder)
    context = await service.claim(view.task_id)
    assert await _code(service.claim(view.task_id)) == "grant_not_active"
    done = await service.record_result(view.task_id, disclosure_id=context.disclosure_id, result=_grounded("Senior Python engineer"), failure=None)
    assert done.phase == "compared" and done.answer is not None and done.answer.kind == "comparison"
    assert await _code(service.record_result(view.task_id, disclosure_id=context.disclosure_id, result=None, failure="invalid_output")) == "disclosure_already_recorded"
    assert await _code(
        service.create_disclosure(view.task_id, document_ids=[view.documents[0].document_id], recipient="openai", model="gpt-x", purpose="again")
    ) == "disclosure_already_made"


@pytest.mark.parametrize(
    "result",
    [
        {"kind": "comparison", "summary": "x", "findings": [{"kind": "match", "text": "t", "evidence": [{"doc_ref": "d1", "quote": "not in the document at all"}]}]},
        {"kind": "comparison", "summary": "x", "findings": [{"kind": "match", "text": "Salary is 999999", "evidence": [{"doc_ref": "d1", "quote": "Senior Python engineer"}]}]},
        {"kind": "comparison", "summary": "x", "findings": [{"kind": "match", "text": "t", "evidence": [{"doc_ref": "d3", "quote": "Senior Python engineer"}]}]},
    ],
)
async def test_an_ungrounded_comparison_fails_and_is_never_retried(service: DocumentService, folder: Path, result: dict[str, Any]) -> None:
    view = await _approved(service, folder)
    context = await service.claim(view.task_id)
    done = await service.record_result(view.task_id, disclosure_id=context.disclosure_id, result=result, failure=None)
    assert done.phase == "failed" and done.disclosure is not None
    assert done.disclosure.error_code in ("answer_not_grounded", "invalid_output")
    assert done.answer is None


async def test_provider_output_with_an_action_or_extra_field_is_refused(service: DocumentService, folder: Path) -> None:
    view = await _approved(service, folder)
    context = await service.claim(view.task_id)
    injected = _grounded("Senior Python engineer") | {"action": "upload", "destination": "https://attacker.example"}
    done = await service.record_result(view.task_id, disclosure_id=context.disclosure_id, result=injected, failure=None)
    assert done.disclosure is not None and done.disclosure.error_code == "invalid_output"


async def test_an_expired_approval_cannot_be_claimed(service: DocumentService, engine: AsyncEngine, folder: Path) -> None:
    view = await _approved(service, folder)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE task_grants SET confirmed_at = now() - interval '1 hour', expires_at = now() - interval '1 second' WHERE kind = 'document_disclose'"))
    assert await _code(service.claim(view.task_id)) == "grant_expired"


async def test_a_document_that_changed_after_the_card_cannot_be_approved(service: DocumentService, engine: AsyncEngine, folder: Path) -> None:
    view = await _two_documents(service, folder)
    ids = [item.document_id for item in view.documents]
    view = await service.create_disclosure(view.task_id, document_ids=ids, recipient="scripted", model="scripted-text", purpose="compare")
    assert view.card is not None
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE documents SET text = NULL, purged_at = now()"))
    assert await _code(service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)) == "document_changed"


async def test_a_claim_whose_runtime_died_is_unknown_and_never_repeated(service: DocumentService, folder: Path) -> None:
    view = await _approved(service, folder)
    context = await service.claim(view.task_id)
    assert await service.recover_started() == 1
    after = await service.describe(view.task_id)
    assert after.phase == "outcome_unknown"
    assert await _code(service.record_result(view.task_id, disclosure_id=context.disclosure_id, result=_grounded("Senior Python engineer"), failure=None)) == "disclosure_already_recorded"
    assert await _code(service.claim(view.task_id)) == "grant_not_active"


async def test_extracted_text_is_purged_after_its_retention(service: DocumentService, engine: AsyncEngine, folder: Path) -> None:
    view = await _two_documents(service, folder)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE documents SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day'"))
    assert await service.sweep_expired() == 2
    after = await service.describe(view.task_id)
    assert all(item.purged and item.preview is None for item in after.documents)
    first, second = (item.document_id for item in after.documents)
    assert await _code(service.compare_local(view.task_id, first_id=first, second_id=second)) == "document_expired"


# ---- S1 adversarial review regressions ------------------------------------------------------------------


@pytest.mark.parametrize("relative", [".git/notes.md", "AppData/Roaming/history.txt", "node_modules/a/readme.md", "~$lock.docx", "a/b/c/d/deep.pdf"])
async def test_a_name_the_listing_would_never_show_cannot_be_added(service: DocumentService, folder: Path, relative: str) -> None:
    target = folder / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(make_text())
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    task = await service.create_task()
    assert await _code(service.add_root_file(task.task_id, root_id=root.root_id, relative_path=relative)) == "not_listable"


async def test_a_root_revoked_during_extraction_leaves_no_document(engine: AsyncEngine, folder: Path) -> None:
    holder: dict[str, Any] = {}

    def extractor(data: bytes, declared: str) -> Any:
        import asyncio as _asyncio

        from app.documents.runner import Extracted

        from sqlalchemy.ext.asyncio import create_async_engine

        async def revoke() -> None:
            # Another connection, as the person's click would be: the revocation commits mid-extraction.
            other = create_async_engine(holder["url"])
            try:
                async with other.begin() as connection:
                    await connection.execute(
                        text("UPDATE file_roots SET status = 'REVOKED', revoked_at = now(), revision = revision + 1 WHERE id = :id"),
                        {"id": holder["root"]},
                    )
            finally:
                await other.dispose()

        _asyncio.run(revoke())
        return Extracted(format=declared, text="synthetic", pages=1, truncated=False)

    service = DocumentService(engine, grant_ttl_seconds=600, protected_folders=((), ()), extractor=extractor)
    holder["url"] = engine.url.render_as_string(hide_password=False)
    root = await service.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
    holder["root"] = root.root_id
    task = await service.create_task()
    view = await service.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
    assert await _code(service.extract(task.task_id, file_id=view.files[0].file_id)) == "root_revoked"
    assert (await service.describe(task.task_id)).documents == ()


async def test_an_approval_cannot_be_claimed_after_the_folder_was_revoked(service: DocumentService, folder: Path) -> None:
    view = await _approved(service, folder)
    root_id = view.files[0].root_id
    assert root_id is not None
    await service.revoke_root(root_id, expected_revision=None)
    assert await _code(service.claim(view.task_id)) == "root_revoked"
    first, second = (item.document_id for item in view.documents)
    assert await _code(service.compare_local(view.task_id, first_id=first, second_id=second)) == "root_revoked"


async def test_retention_also_expires_comparison_quotes_and_dropped_paths(service: DocumentService, engine: AsyncEngine, folder: Path) -> None:
    view = await _approved(service, folder)
    context = await service.claim(view.task_id)
    await service.record_result(view.task_id, disclosure_id=context.disclosure_id, result=_grounded("Senior Python engineer"), failure=None)
    dropped = await service.create_task()
    await service.add_dropped_file(dropped.task_id, path=str((folder / "cover.docx").resolve()), display_name="cover.docx")
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE document_answers SET created_at = now() - interval '2 days'"))
        await connection.execute(text("UPDATE file_refs SET created_at = now() - interval '2 days'"))
    after = await service.describe(view.task_id)
    assert after.answer is not None and after.answer.findings == [] and after.answer.summary == "Expired."
    async with engine.connect() as connection:
        paths = (await connection.execute(text("SELECT local_path FROM file_refs WHERE source = 'DROPPED_FILE'"))).scalars().all()
    assert paths == [""]
