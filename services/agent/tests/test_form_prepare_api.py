"""Milestone 8b S5 over HTTP: saved details never echo, and the generic routes cannot
mint or move a form-disclosure approval."""

import uuid
from typing import Any

import httpx
import pytest

from app.domain.protected_values import PROTECTED_KINDS

SECRET = "EMAIL_SECRET_S5_82B@example.test"


async def test_saving_a_detail_returns_kind_and_preview_and_never_the_value(client: httpx.AsyncClient) -> None:
    response = await client.put("/protected-values/email", json={"value": SECRET})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["kind"] == "email" and body["data_ref"] == "email" and body["preview"] == "E***@e***.test"
    assert set(body) == {"data_ref", "kind", "preview", "updated_at"}
    assert SECRET not in response.text
    listed = await client.get("/protected-values")
    assert listed.status_code == 200 and SECRET not in listed.text
    assert [item["kind"] for item in listed.json()["details"]] == ["email"]


async def test_the_saved_detail_route_is_closed(client: httpx.AsyncClient) -> None:
    for kind in ("password", "custom", "otp", "Email", "email%20"):
        response = await client.put(f"/protected-values/{kind}", json={"value": "x@example.test"})
        assert response.status_code == 422 and response.json()["error"]["code"] == "protected_value_refused", kind
        assert "x@example.test" not in response.text
    for body in ({"value": "a@example.test", "kind": "email"}, {"value": "a@example.test", "preview": "p"}, {}, {"value": 5}):
        assert (await client.put("/protected-values/email", json=body)).status_code == 422
    refused = await client.put("/protected-values/email", json={"value": "not-an-email"})
    assert refused.status_code == 422 and refused.json()["error"]["reason"] == "invalid_email"
    assert "not-an-email" not in refused.text
    assert len(PROTECTED_KINDS) == 8


async def test_there_is_no_route_to_read_a_saved_value(client: httpx.AsyncClient) -> None:
    await client.put("/protected-values/city", json={"value": "CITY_SECRET_S5_A11"})
    for path in ("/protected-values/city", "/protected-values/city/value", "/protected-values?kind=city&raw=1"):
        response = await client.get(path)
        assert "CITY_SECRET_S5_A11" not in response.text, path


async def test_the_generic_route_cannot_propose_a_form_disclosure(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": {"type": "appointment_booking", "text": "x"}})).json()
    response = await client.post(
        f"/tasks/{task['id']}/actions",
        json={"idempotency_key": "k-1", "tool_name": "prepare_form", "risk_tier": "R2", "proposal": {"fields": []}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["reason"] == "use_disclosure_route"
    listed = await client.get(f"/tasks/{task['id']}/actions")
    assert listed.json()["actions"] == []


async def test_disclosure_routes_refuse_unknown_actions_and_stay_narrow(client: httpx.AsyncClient) -> None:
    missing = uuid.uuid4()
    for verb in ("approve", "reject"):
        response = await client.post(f"/actions/{missing}/field-disclosure/{verb}", json={"expected_revision": 1})
        assert response.status_code == 404, verb
    # An id and a revision only: a manifest, a value, an origin, a field or a provider is refused.
    extras: list[dict[str, Any]] = [{"manifest": {}}, {"value": "x"}, {"origin": "https://evil.test"}, {"fields": []}, {"provider": "openai"}]
    for extra in extras:
        response = await client.post(f"/actions/{missing}/field-disclosure/approve", json={"expected_revision": 1, **extra})
        assert response.status_code == 422, extra
    assert (await client.post(f"/actions/{missing}/field-disclosure/approve", json={})).status_code == 422


async def test_a_booking_action_cannot_be_approved_through_the_disclosure_route(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": {"type": "appointment_booking", "text": "x"}})).json()
    action = (await client.post(
        f"/tasks/{task['id']}/actions",
        json={"idempotency_key": "k-2", "tool_name": "commit_booking", "risk_tier": "R2", "proposal": {"a": 1}},
    )).json()
    response = await client.post(f"/actions/{action['id']}/field-disclosure/approve", json={"expected_revision": action["revision"]})
    assert response.status_code == 422 and response.json()["error"]["reason"] == "not_a_disclosure_approval"
    after = (await client.get(f"/actions/{action['id']}")).json()
    assert after["status"] == "PROPOSED"


@pytest.mark.parametrize("path", ["approve", "reject"])
async def test_the_disclosure_routes_need_the_runtime_token(settings: Any, path: str) -> None:
    from app.main import create_app
    from tests.conftest import running_app

    async with running_app(create_app(settings)) as authorised:
        transport = authorised._transport  # noqa: SLF001
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as anonymous:
            response = await anonymous.post(f"/actions/{uuid.uuid4()}/field-disclosure/{path}", json={"expected_revision": 1})
            assert response.status_code in (401, 403)
