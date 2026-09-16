import uuid

import httpx
from pydantic import SecretStr
from starlette.types import Message

from app.api.security import RuntimeSecurityMiddleware

from tests.conftest import TEST_RUNTIME_TOKEN


def auth_header(token: str = TEST_RUNTIME_TOKEN.get_secret_value()) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_every_http_path_requires_authentication(client: httpx.AsyncClient) -> None:
    paths = [
        ("GET", "/health"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
        ("POST", "/tasks"),
        ("POST", "/lifecycle/shutdown"),
        ("GET", "/not-a-route"),
    ]
    for method, path in paths:
        response = await client.request(method, path, headers={"Authorization": ""})
        assert response.status_code == 401, path
        assert response.json() == {
            "error": {"code": "authentication_required", "message": "Request refused."}
        }
        assert response.headers["www-authenticate"] == "Bearer"


async def test_wrong_credentials_are_safely_refused(
    client: httpx.AsyncClient,
) -> None:
    token = "wrong-token"
    response = await client.get("/health", headers=auth_header(token))
    assert response.status_code == 401
    assert token not in response.text


async def test_non_ascii_raw_credential_is_safely_refused() -> None:
    async def inner(_scope: object, _receive: object, _send: object) -> None:
        raise AssertionError("malformed credential reached the application")

    middleware = RuntimeSecurityMiddleware(
        inner, token=SecretStr(TEST_RUNTIME_TOKEN.get_secret_value())
    )
    sent: list[Message] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [(b"host", b"127.0.0.1"), (b"authorization", b"Bearer \xff")],
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 401


async def test_host_and_origin_are_restricted(client: httpx.AsyncClient) -> None:
    wrong_host = await client.get("/health", headers={**auth_header(), "Host": "evil.test"})
    assert wrong_host.status_code == 400
    assert wrong_host.json()["error"]["code"] == "invalid_host"

    origin = await client.get(
        "/health", headers={**auth_header(), "Origin": "http://127.0.0.1:5173"}
    )
    assert origin.status_code == 403
    assert origin.json()["error"]["code"] == "origin_not_allowed"


async def test_authentication_precedes_routing(client: httpx.AsyncClient) -> None:
    assert (await client.get("/missing", headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/missing", headers=auth_header())).status_code == 404


async def test_validation_errors_do_not_echo_rejected_input(client: httpx.AsyncClient) -> None:
    secret_text = "private-proposal-value"
    response = await client.post(
        "/tasks",
        json={"request": {"type": "invalid type", "notes": secret_text}},
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Request validation failed."}
    }
    assert secret_text not in response.text


async def test_health_returns_the_authenticated_process_generation(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert uuid.UUID(response.json()["runtime_generation"])
