"""A synthetic "site with sessions and a login flow" for Milestone 8a.

Evaluation infrastructure, not Lumi application code.

S1 needed only enough of this fixture to prove a persistent Chromium profile
kept a session without Lumi ever reading a cookie. S2 needs the rest of what
a real sign-in looks like, entirely synthetic:

    GET/POST /login              username + password, a real `type=password`
                                  field
    GET/POST /login/otp          a second factor, `autocomplete=one-time-code`
    GET      /login/sso          redirects to a *second* fixture origin (the
                                  "identity provider") and back
    GET      /idp/authorize      the identity-provider side of that chain
    GET      /login/sso/callback where the chain lands, on this origin
    GET/POST /login/challenge    a CAPTCHA-shaped surface a human clicks through
    GET      /login/offsite      leaves this origin and never returns, for the
                                  "takeover ended off the profile's site" case
    GET/POST /switch-account     signs in as a second, different account
    GET      /session/expire     invalidates the current session server-side
    GET      /logout             the same, redirecting home

None of this is a real identity provider or a real CAPTCHA. `FIXTURE_USERNAME`,
`FIXTURE_PASSWORD` and `FIXTURE_OTP` are synthetic values that exist only in
this file and the tests that plant them; they authorise nothing outside this
process's own in-memory `issued` table. `/idp/authorize` does not check
anything at all -- it exists to exercise a browser-followed redirect chain
through a second public-looking origin during a human takeover, not to model
authentication logic a real identity provider would have.

The signed-in `/account` page carries a `data-lumi-account-id` attribute: the
one, fixed, documented convention `app/browser/credential_signals.py` reads to
derive an account fingerprint. A real site's equivalent signal is an open
design question left to Milestone 8a S3; this fixture exists to prove the
mechanism deterministically, not to claim every site exposes one.
"""

import secrets
from html import escape
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SESSION_COOKIE = "lumi_fixture_session"
ACCOUNT_NAME = "Fixture Account"
ACCOUNT_ID = "fixture-account-1"
SECOND_ACCOUNT_NAME = "Second Account"
SECOND_ACCOUNT_ID = "fixture-account-2"
#: What a page says when the server does not recognise the browser. The tests
#: assert on these exact strings, so they are constants rather than prose.
SIGNED_IN = "signed in"
SIGNED_OUT = "signed out"

#: Synthetic-only. Never a real credential; planted here and in tests, and
#: asserted absent from the database, runtime responses, logs and diagnostics.
FIXTURE_USERNAME = "fixture-user"
FIXTURE_PASSWORD = "S2-Fixture-Passw0rd-Only"  # noqa: S105 - synthetic test value
FIXTURE_OTP = "482913"  # noqa: S105 - synthetic test value
#: The SSO chain's bearer, equally synthetic and equally planted/scanned-for.
SSO_TOKEN = "demo-sso-token"  # noqa: S105 - synthetic test value


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"</head><body>{body}</body></html>"
    )


def create_site(*, idp_origin: str | None = None) -> FastAPI:
    """Build the fixture app.

    `idp_origin` names the *second* fixture origin `/login/sso` redirects to.
    Every instance also serves `/idp/authorize`, so the same factory can play
    either role -- the "account" origin in an SSO test names the other
    instance's origin here; the "identity provider" instance does not need
    `idp_origin` at all, because nothing on this origin ever redirects away
    from itself except through that one configured chain.
    """
    app = FastAPI(title="Lumi account fixture", docs_url=None, redoc_url=None)
    router = APIRouter()
    # One secret per server process. A restart of the *fixture* invalidates
    # every session, which is what makes "the browser kept it" the only
    # explanation when the fixture stays up across a worker restart.
    issued: dict[str, str] = {}
    identities: dict[str, str] = {}
    hits: dict[str, int] = {}

    def _count(path: str) -> None:
        hits[path] = hits.get(path, 0) + 1

    def _sign_in(response: Response, *, name: str, identity: str) -> None:
        token = secrets.token_urlsafe(16)
        issued[token] = name
        identities[token] = identity
        # HttpOnly: not readable by page script either, so the only thing
        # that can present it later is the browser itself.
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=86_400, path="/"
        )

    def _current_name(request: Request) -> str | None:
        token = request.cookies.get(SESSION_COOKIE)
        return issued.get(token or "")

    async def _urlencoded_form(request: Request) -> dict[str, str]:
        """A minimal `application/x-www-form-urlencoded` reader.

        Avoids depending on `python-multipart` (which Starlette's own
        `Request.form()` requires even for this simple, non-multipart case)
        purely to keep this test fixture's own dependency footprint out of
        the packaged runtime. This fixture's forms never use file inputs.
        """
        body = (await request.body()).decode("utf-8", errors="replace")
        return dict(parse_qsl(body, keep_blank_values=True))

    @router.get("/")
    async def index() -> HTMLResponse:
        _count("/")
        return _page(
            "Account fixture",
            "<h1>Account fixture</h1>"
            "<p><a href='/session/start'>Start a session</a></p>"
            "<p><a href='/login'>Sign in</a></p>"
            "<p><a href='/account'>Account</a></p>",
        )

    @router.get("/session/start")
    @router.post("/session/start")
    async def start_session(response: Response) -> HTMLResponse:
        """Issue a synthetic session cookie directly. Kept for the S1
        persistence proof, which does not drive the login form."""
        _count("/session/start")
        page = _page(
            "Session started",
            f"<h1>Session started</h1><p>You are {SIGNED_IN} as {escape(ACCOUNT_NAME)}.</p>",
        )
        _sign_in(page, name=ACCOUNT_NAME, identity=ACCOUNT_ID)
        return page

    @router.get("/account")
    async def account(request: Request) -> HTMLResponse:
        """The proof page. The *server* decides which of the two texts appears."""
        _count("/account")
        name = _current_name(request)
        if name is None:
            return _page("Account", f"<h1>Account</h1><p>You are {SIGNED_OUT}.</p>")
        token = request.cookies.get(SESSION_COOKIE, "")
        identity = identities.get(token, "")
        return _page(
            "Account",
            f"<h1>Account</h1><p>You are {SIGNED_IN} as {escape(name)}.</p>"
            f"<div data-lumi-account-id='{escape(identity)}' style='display:none'></div>",
        )

    # ---- login: password -----------------------------------------------------

    @router.get("/login")
    async def login_form(request: Request) -> HTMLResponse:
        _count("/login")
        error = "wrong" in request.query_params
        return _page(
            "Sign in",
            "<h1>Sign in</h1>"
            + ("<p id='login-error'>Incorrect username or password.</p>" if error else "")
            + "<form method='post' action='/login'>"
            "<input type='text' name='username' autocomplete='username'>"
            "<input type='password' name='password' autocomplete='current-password'>"
            "<button type='submit'>Continue</button>"
            "</form>",
        )

    @router.post("/login")
    async def login_submit(request: Request) -> Response:
        _count("/login.submit")
        form = await _urlencoded_form(request)
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if username == FIXTURE_USERNAME and password == FIXTURE_PASSWORD:
            return RedirectResponse("/login/otp", status_code=303)
        return RedirectResponse("/login?wrong=1", status_code=303)

    # ---- login: one-time code -------------------------------------------------

    @router.get("/login/otp")
    async def otp_form(request: Request) -> HTMLResponse:
        _count("/login/otp")
        error = "wrong" in request.query_params
        return _page(
            "Enter your code",
            "<h1>Enter your code</h1>"
            + ("<p id='otp-error'>Incorrect code.</p>" if error else "")
            + "<form method='post' action='/login/otp'>"
            "<input type='text' name='code' autocomplete='one-time-code'>"
            "<button type='submit'>Verify</button>"
            "</form>",
        )

    @router.post("/login/otp")
    async def otp_submit(request: Request) -> Response:
        _count("/login/otp.submit")
        form = await _urlencoded_form(request)
        code = str(form.get("code", ""))
        if code == FIXTURE_OTP:
            response = RedirectResponse("/account", status_code=303)
            _sign_in(response, name=ACCOUNT_NAME, identity=ACCOUNT_ID)
            return response
        return RedirectResponse("/login/otp?wrong=1", status_code=303)

    # ---- login: a CAPTCHA-shaped challenge, human-only ------------------------

    @router.get("/login/challenge")
    async def challenge_form() -> HTMLResponse:
        _count("/login/challenge")
        return _page(
            "Verify you're human",
            "<h1>Verify you're human</h1>"
            "<div class='challenge' data-challenge-provider='lumi-fixture'>"
            "This is a synthetic challenge. A human clicks through it; Lumi never sees it."
            "</div>"
            "<form method='post' action='/login/challenge'>"
            "<button type='submit' id='challenge-continue'>I am human</button>"
            "</form>",
        )

    @router.post("/login/challenge")
    async def challenge_submit() -> Response:
        _count("/login/challenge.submit")
        response = RedirectResponse("/account", status_code=303)
        _sign_in(response, name=ACCOUNT_NAME, identity=ACCOUNT_ID)
        return response

    # ---- login: SSO redirect chain through a second fixture origin -----------

    @router.get("/login/sso")
    async def sso_start(request: Request) -> Response:
        _count("/login/sso")
        if idp_origin is None:
            return _page("SSO not configured", "<h1>SSO not configured on this fixture instance</h1>")
        own_origin = f"{request.url.scheme}://{request.url.netloc}"
        callback = quote(f"{own_origin}/login/sso/callback", safe="")
        return RedirectResponse(f"{idp_origin}/idp/authorize?return_to={callback}", status_code=302)

    @router.get("/login/sso/callback")
    async def sso_callback(request: Request) -> Response:
        _count("/login/sso/callback")
        token = request.query_params.get("sso_token", "")
        if token != SSO_TOKEN:
            return RedirectResponse("/login?wrong=1", status_code=303)
        response = RedirectResponse("/account", status_code=303)
        _sign_in(response, name=ACCOUNT_NAME, identity=ACCOUNT_ID)
        return response

    @router.get("/idp/authorize")
    async def idp_authorize(request: Request) -> Response:
        """The identity-provider side. Checks nothing: the point of this
        route is the browser-followed redirect chain, not authentication
        logic a real IdP would have."""
        _count("/idp/authorize")
        return_to = request.query_params.get("return_to", "")
        if not return_to:
            return _page("Bad request", "<h1>Missing return_to</h1>")
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(f"{return_to}{separator}sso_token={SSO_TOKEN}", status_code=302)

    # ---- ending up somewhere other than the profile's own site ---------------

    @router.get("/login/offsite")
    async def offsite(request: Request) -> Response:
        """Leaves this origin and never returns -- for the
        `login_not_on_profile_site` case. Redirects to whatever origin was
        named, which in tests is the second fixture instance's bare `/`."""
        _count("/login/offsite")
        destination = request.query_params.get("to", "")
        if not destination:
            return _page("Bad request", "<h1>Missing destination</h1>")
        return RedirectResponse(destination, status_code=302)

    # ---- account switching, logout, session expiry ----------------------------

    @router.get("/switch-account")
    async def switch_account() -> Response:
        """Signs in as a *different* account, with a different identity
        signal, so a test can show the fingerprint changes."""
        _count("/switch-account")
        response = RedirectResponse("/account", status_code=303)
        _sign_in(response, name=SECOND_ACCOUNT_NAME, identity=SECOND_ACCOUNT_ID)
        return response

    @router.get("/session/expire")
    async def expire_session(request: Request) -> HTMLResponse:
        """Invalidates the session server-side without clearing the cookie:
        the next `/account` request from the same browser is signed out,
        exactly as a real session expiry would look."""
        _count("/session/expire")
        token = request.cookies.get(SESSION_COOKIE)
        if token is not None:
            issued.pop(token, None)
            identities.pop(token, None)
        return _page("Session expired", "<h1>Session expired</h1><p>You are signed out.</p>")

    @router.get("/logout")
    async def logout(request: Request) -> Response:
        _count("/logout")
        token = request.cookies.get(SESSION_COOKIE)
        if token is not None:
            issued.pop(token, None)
            identities.pop(token, None)
        return RedirectResponse("/", status_code=303)

    # ---- web storage persistence proof (unchanged from S1) --------------------

    @router.get("/storage/write")
    async def storage_write() -> HTMLResponse:
        _count("/storage/write")
        return _page(
            "Storage written",
            "<h1>Storage written</h1><p id='state'>writing</p>"
            "<script>localStorage.setItem('lumi_fixture_marker', 'kept');"
            "document.getElementById('state').textContent = 'written';</script>",
        )

    @router.get("/storage/read")
    async def storage_read() -> HTMLResponse:
        _count("/storage/read")
        return _page(
            "Storage read",
            "<h1>Storage read</h1><p id='state'>reading</p>"
            "<script>document.getElementById('state').textContent = "
            "localStorage.getItem('lumi_fixture_marker') === 'kept' ? 'kept' : 'missing';"
            "</script>",
        )

    # ---- test control plane. Lumi never calls this -----------------------------

    @router.get("/__eval__/state")
    async def state() -> JSONResponse:
        return JSONResponse({"hits": hits, "sessions": len(issued)})

    @router.post("/__eval__/reset")
    async def reset() -> JSONResponse:
        issued.clear()
        identities.clear()
        hits.clear()
        return JSONResponse({"reset": True})

    app.include_router(router)
    return app


__all__ = [
    "ACCOUNT_ID",
    "ACCOUNT_NAME",
    "FIXTURE_OTP",
    "FIXTURE_PASSWORD",
    "FIXTURE_USERNAME",
    "SECOND_ACCOUNT_ID",
    "SECOND_ACCOUNT_NAME",
    "SESSION_COOKIE",
    "SIGNED_IN",
    "SIGNED_OUT",
    "SSO_TOKEN",
    "create_site",
]
