"""Milestone 10 S1 over HTTP: every response is checked for the absolute path it must never contain."""

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.conftest import running_app
from tests.document_fixtures import make_pdf, make_text

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the M10 file broker ships on Windows")


@pytest.fixture
async def api(settings: Settings, engine: object, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    # Roots live under the system temp folder (inside LocalAppData); the real protected set is tested in
    # test_file_broker.py.
    monkeypatch.setattr("app.files.broker._protected", lambda: ((), ()))
    quiet = settings.model_copy(update={"public_inspection_hosts": "", "inspection_test_origins": ""})
    async with running_app(create_app(quiet)) as client:
        yield client


def _clean(response: httpx.Response, *forbidden: str) -> dict[str, Any]:
    body = response.text
    for value in forbidden:
        assert value.casefold() not in body.casefold(), "an absolute path leaked into a response"
        assert json.dumps(value)[1:-1].casefold() not in body.casefold(), "an escaped absolute path leaked"
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


async def test_the_whole_s1_flow_never_returns_an_absolute_path(api: httpx.AsyncClient, tmp_path: Path) -> None:
    root_dir = tmp_path / "Approved"
    root_dir.mkdir()
    (root_dir / "resume.pdf").write_bytes(make_pdf())
    (root_dir / "job.txt").write_bytes(make_text())
    secrets = (str(tmp_path), str(tmp_path.resolve()))

    registered = _clean(
        await api.post("/file-roots", json={"path": str(root_dir), "label": "Approved", "can_read": True, "can_create": False}),
        *secrets,
    )
    root_id = registered["root_id"]
    _clean(await api.get("/file-roots"), *secrets)
    listing = _clean(await api.get(f"/file-roots/{root_id}/files"), *secrets)
    assert {item["relative_path"] for item in listing["files"]} == {"resume.pdf", "job.txt"}

    task = _clean(await api.post("/document-tasks", json={"objective": "compare"}), *secrets)
    task_id = task["task_id"]
    for name in ("resume.pdf", "job.txt"):
        task = _clean(await api.post(f"/document-tasks/{task_id}/files", json={"root_id": root_id, "relative_path": name}), *secrets)
    for item in task["files"]:
        task = _clean(await api.post(f"/document-tasks/{task_id}/extract", json={"file_id": item["file_id"]}), *secrets)
    ids = [item["document_id"] for item in task["documents"]]
    _clean(await api.post(f"/document-tasks/{task_id}/compare", json={"first_document_id": ids[0], "second_document_id": ids[1]}), *secrets)
    card = _clean(
        await api.post(
            f"/document-tasks/{task_id}/disclosure",
            json={"document_ids": ids, "recipient": "scripted", "model": "scripted-text", "purpose": "How well do I match?"},
        ),
        *secrets,
    )
    grant = card["card"]
    assert isinstance(grant, dict)
    _clean(
        await api.post(f"/document-tasks/{task_id}/disclosure/grant", json={"grant_id": grant["grant_id"], "expected_revision": grant["grant_revision"]}),
        *secrets,
    )
    claim = _clean(await api.post(f"/document-tasks/{task_id}/disclosure/claim"), *secrets)
    assert "resume.pdf" not in json.dumps(claim["projection"]) and "job.txt" not in json.dumps(claim["projection"])
    latest = _clean(await api.get("/document-tasks/latest"), *secrets)
    assert latest["task"] is not None


@pytest.mark.parametrize("relative", ["../x.pdf", "C:\\Windows\\win.ini", "\\\\server\\share\\a.pdf", "a.pdf:ads"])
async def test_an_escaping_name_is_a_422_with_a_code_and_no_echo(api: httpx.AsyncClient, tmp_path: Path, relative: str) -> None:
    root_dir = tmp_path / "Approved"
    root_dir.mkdir()
    root = (await api.post("/file-roots", json={"path": str(root_dir), "label": "A", "can_read": True, "can_create": False})).json()
    task = (await api.post("/document-tasks", json={})).json()
    response = await api.post(f"/document-tasks/{task['task_id']}/files", json={"root_id": root["root_id"], "relative_path": relative})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "document_refused"
    assert relative not in response.text


async def test_a_file_changed_after_it_was_added_is_a_409_state_change(api: httpx.AsyncClient, tmp_path: Path) -> None:
    root_dir = tmp_path / "Approved"
    root_dir.mkdir()
    (root_dir / "job.txt").write_bytes(make_text())
    root = (await api.post("/file-roots", json={"path": str(root_dir), "label": "A", "can_read": True, "can_create": False})).json()
    task = (await api.post("/document-tasks", json={})).json()
    task = (await api.post(f"/document-tasks/{task['task_id']}/files", json={"root_id": root["root_id"], "relative_path": "job.txt"})).json()
    (root_dir / "job.txt").write_bytes(make_text(["something else entirely"]))
    response = await api.post(f"/document-tasks/{task['task_id']}/extract", json={"file_id": task["files"][0]["file_id"]})
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "document_state_changed",
        "message": "That document is no longer available as approved.",
        "reason": "file_changed",
    }


async def test_modify_permission_cannot_be_requested(api: httpx.AsyncClient, tmp_path: Path) -> None:
    response = await api.post("/file-roots", json={"path": str(tmp_path), "label": "A", "can_read": True, "can_create": False, "can_modify": True})
    assert response.status_code == 422
    assert response.json()["error"]["reason"] == "modify_not_supported"


async def test_unknown_fields_are_refused(api: httpx.AsyncClient) -> None:
    response = await api.post("/document-tasks", json={"objective": "x", "path": "C:\\"})
    assert response.status_code == 422
