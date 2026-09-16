"""The authentication boundary for Electron main -> agent runtime traffic."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import SecretStr
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

AUTH_SCHEME = "Bearer"
LOOPBACK_HOST = "127.0.0.1"


def authorization_header(token: SecretStr) -> str:
    return f"{AUTH_SCHEME} {token.get_secret_value()}"


def _token_matches(presented: str | None, expected: SecretStr) -> bool:
    if presented is None:
        return False
    scheme, separator, token = presented.partition(" ")
    if separator != " " or scheme.lower() != AUTH_SCHEME.lower() or not token:
        return False
    try:
        presented_bytes = token.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(presented_bytes, expected.get_secret_value().encode("ascii"))


def _host_name(host_header: str | None) -> str | None:
    if not host_header:
        return None
    # This runtime deliberately has no IPv6 bind. A colon therefore only
    # separates the numeric loopback host from its port.
    host, _, port = host_header.partition(":")
    if host != LOOPBACK_HOST:
        return None
    if port and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65_535):
        return None
    return host


class RuntimeSecurityMiddleware:
    """Authenticate every protocol before routing or request-body parsing.

    A single ASGI boundary also covers future streaming and WebSocket routes;
    adding a router cannot accidentally omit the security dependency.
    """

    def __init__(self, app: ASGIApp, *, token: SecretStr) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if _host_name(headers.get("host")) is None:
            await self._reject(scope, receive, send, 400, "invalid_host")
            return
        # Electron main's HTTP client does not send Origin. Rejecting every
        # supplied Origin is the smallest allowlist and defence in depth; the
        # mandatory bearer credential remains the actual authentication bound.
        if headers.get("origin") is not None:
            await self._reject(scope, receive, send, 403, "origin_not_allowed")
            return
        if not _token_matches(headers.get("authorization"), self._token):
            await self._reject(
                scope,
                receive,
                send,
                401,
                "authentication_required",
                [(b"www-authenticate", b"Bearer")],
            )
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        code: str,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": code})
            return
        body = ('{"error":{"code":"%s","message":"Request refused."}}' % code).encode()
        response_headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if headers:
            response_headers.extend(headers)
        await send({"type": "http.response.start", "status": status_code, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})
