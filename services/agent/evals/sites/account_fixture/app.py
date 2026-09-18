"""A tiny "site with sessions" for proving that a Lumi profile persists.

Evaluation infrastructure, not Lumi application code.

The point of this fixture is narrow and it matters: it lets a test prove that a
persistent Chromium profile kept its session **without Lumi ever reading a
cookie**. The proof is the server's own behaviour, not an export:

    POST /session/start      -> the server issues a session cookie
                                (HttpOnly, so page script cannot read it either)
    ... browser worker is killed and restarted, or the whole runtime is ...
    GET  /account            -> the server sees the cookie and the page says
                                "signed in", with the synthetic account name

If the profile had not persisted, `/account` would render "signed out". Nothing
in Lumi calls `storage_state()`, `context.cookies()` or reads a file under the
profile directory to establish this, and there is a source-level test
(`tests/test_no_credential_extraction.py`) that fails if anybody adds such a
call. The browser holds the credential; the server confirms it; Lumi only reads
the visible text of a page it was allowed to open.

The session value is synthetic, random per server process, and grants access to
nothing but this fixture's own `/account` page. It is not a credential for
anything real, and `/__eval__/*` is a test control plane Lumi never calls.

`/storage/*` does the same proof for `localStorage` rather than a cookie, so a
site that keeps its session in web storage is covered too.
"""

import secrets
from html import escape

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

SESSION_COOKIE = "lumi_fixture_session"
ACCOUNT_NAME = "Fixture Account"
#: What a page says when the server does not recognise the browser. The tests
#: assert on these exact strings, so they are constants rather than prose.
SIGNED_IN = "signed in"
SIGNED_OUT = "signed out"


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"</head><body>{body}</body></html>"
    )


def create_site() -> FastAPI:
    app = FastAPI(title="Lumi account fixture", docs_url=None, redoc_url=None)
    router = APIRouter()
    # One secret per server process. A restart of the *fixture* invalidates
    # every session, which is what makes "the browser kept it" the only
    # explanation when the fixture stays up across a worker restart.
    issued: dict[str, str] = {}
    hits: dict[str, int] = {}

    def _count(path: str) -> None:
        hits[path] = hits.get(path, 0) + 1

    @router.get("/")
    async def index() -> HTMLResponse:
        _count("/")
        return _page(
            "Account fixture",
            "<h1>Account fixture</h1>"
            "<p><a href='/session/start'>Start a session</a></p>"
            "<p><a href='/account'>Account</a></p>",
        )

    @router.get("/session/start")
    @router.post("/session/start")
    async def start_session(response: Response) -> HTMLResponse:
        """Issue a synthetic session cookie, the way a real sign-in would."""
        _count("/session/start")
        token = secrets.token_urlsafe(16)
        issued[token] = ACCOUNT_NAME
        page = _page(
            "Session started",
            f"<h1>Session started</h1><p>You are {SIGNED_IN} as {escape(ACCOUNT_NAME)}.</p>",
        )
        # HttpOnly: not readable by page script either, so the only thing that
        # can present it later is the browser itself.
        page.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="lax",
            max_age=86_400,
            path="/",
        )
        return page

    @router.get("/account")
    async def account(request: Request) -> HTMLResponse:
        """The proof page. The *server* decides which of the two texts appears."""
        _count("/account")
        token = request.cookies.get(SESSION_COOKIE)
        name = issued.get(token or "")
        if name is None:
            return _page("Account", f"<h1>Account</h1><p>You are {SIGNED_OUT}.</p>")
        return _page(
            "Account",
            f"<h1>Account</h1><p>You are {SIGNED_IN} as {escape(name)}.</p>",
        )

    @router.get("/storage/write")
    async def storage_write() -> HTMLResponse:
        """Put a synthetic marker in `localStorage`, from the page itself."""
        _count("/storage/write")
        return _page(
            "Storage written",
            "<h1>Storage written</h1><p id='state'>writing</p>"
            "<script>localStorage.setItem('lumi_fixture_marker', 'kept');"
            "document.getElementById('state').textContent = 'written';</script>",
        )

    @router.get("/storage/read")
    async def storage_read() -> HTMLResponse:
        """Report whether the marker survived, again from the page itself."""
        _count("/storage/read")
        return _page(
            "Storage read",
            "<h1>Storage read</h1><p id='state'>reading</p>"
            "<script>document.getElementById('state').textContent = "
            "localStorage.getItem('lumi_fixture_marker') === 'kept' ? 'kept' : 'missing';"
            "</script>",
        )

    @router.get("/__eval__/state")
    async def state() -> JSONResponse:
        """Test control plane. Lumi never calls this."""
        return JSONResponse({"hits": hits, "sessions": len(issued)})

    @router.post("/__eval__/reset")
    async def reset() -> JSONResponse:
        issued.clear()
        hits.clear()
        return JSONResponse({"reset": True})

    app.include_router(router)
    return app


__all__ = ["ACCOUNT_NAME", "SESSION_COOKIE", "SIGNED_IN", "SIGNED_OUT", "create_site"]
