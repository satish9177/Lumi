"""Milestone 8b S4: synthetic forms for observation only.

Nothing here is a real application form, and nothing in Lumi may change one. The
pages exist so a test can prove two things at once:

* what an element projection **carries** (roles, names, options, flags), and
* what it **never** carries (`CURRENT_VALUE_SECRET`, an option's raw `value=`, a
  control's `id`, `name` or `class`, a cross-origin frame's label, a file,
  password or one-time-code control).

Every listener in `_EVENT_PROBE` only *counts*. `window.__lumiForm` therefore shows
whether anything interacted with a control while it was observed: `input`,
`change`, `focus`, `click`, `keydown`, `submit` and `autosave` must all stay 0.
The submit handler cancels the submission, and the `POST /app/apply/submit` route
counts the request that would have been made -- so a submission is caught twice.
"""

from collections.abc import Callable
from html import escape
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

#: Planted on the form to prove what a projection never carries.
CURRENT_VALUE_SECRET = "CURRENT_VALUE_SECRET_71A"
OPTION_VALUE_SECRET = "OPTION_VALUE_SECRET_82B"
CONTROL_ID_SECRET = "CONTROL_ID_SECRET_93C"
CONTROL_NAME_SECRET = "CONTROL_NAME_SECRET_A4D"
CONTROL_CLASS_SECRET = "CONTROL_CLASS_SECRET_B5E"
CROSS_ORIGIN_FIELD_SECRET = "CROSS_ORIGIN_FIELD_SECRET_C6F"
#: Labels of controls that must never receive a ref.
FILE_LABEL_SECRET = "FILE_LABEL_SECRET_D7A"
PASSWORD_LABEL_SECRET = "PASSWORD_LABEL_SECRET_E8B"
OTP_LABEL_SECRET = "OTP_LABEL_SECRET_F9C"
#: Present in an `aria-label`, so it is in the *inventory* and never in the page's
#: visible text: the marker that proves where the inventory does and does not go.
FORM_FIELD_SECRET_MARKER = "FORM_FIELD_SECRET_MARKER_0AD"
SECURE_FRAME_SIBLING_SECRET = "SECURE_FRAME_SIBLING_SECRET_1BE"
FRAME_FIELD_LABEL = "Frame employer"

EVENT_PROBE = (
    "<script>window.__lumiForm={input:0,change:0,focus:0,blur:0,click:0,keydown:0,"
    "submit:0,autosave:0};"
    "const bump=(k)=>{window.__lumiForm[k]+=1;};"
    "for(const t of ['input','change']) document.addEventListener(t,()=>{bump(t);bump('autosave');},true);"
    "document.addEventListener('focusin',()=>bump('focus'),true);"
    "document.addEventListener('focusout',()=>bump('blur'),true);"
    "document.addEventListener('click',()=>bump('click'),true);"
    "document.addEventListener('keydown',()=>bump('keydown'),true);"
    "document.addEventListener('submit',(e)=>{bump('submit');e.preventDefault();},true);"
    "</script>"
)

_REACT_SCRIPT = (
    "<script>"
    "const before=()=>`<label>Full name <input type='text' value='CURRENT_VALUE_SECRET_71A'></label>"
    "<label>Email <input type='email'></label>`;"
    "const after=()=>`<label>Full name <input type='text'></label>"
    "<label>Work email <input type='email'></label>"
    "<label>Country <select><option value='in'>India</option>"
    "<option value='us'>United States</option></select></label>`;"
    "window.lumiRerender=(mode)=>{const root=document.getElementById('root');"
    "root.innerHTML=(mode==='changed')?after():before();};"
    "window.lumiRerender('initial');"
    "</script>"
)


def add_form_routes(
    router: APIRouter,
    *,
    page: Callable[[str, str], HTMLResponse],
    account_page: Callable[..., Response],
    count: Callable[[str], None],
    effects: dict[str, int],
    external_origin: str | None,
    planted_email: str,
    planted_long_id: str,
    injection_text: str,
) -> None:
    @router.get("/app/apply")
    async def app_apply(request: Request) -> Response:
        count("/app/apply")
        cross = (
            f"<iframe title='cross' src='{escape(external_origin)}/xframe'></iframe>"
            if external_origin
            else ""
        )
        return account_page(
            request,
            "Apply",
            "<h1>Application</h1>"
            f"<p>{injection_text}</p>"
            "<form method='post' action='/app/apply/submit' aria-label='Application'>"
            f"<label for='{CONTROL_ID_SECRET}'>Full legal name</label>"
            f"<input type='text' id='{CONTROL_ID_SECRET}' name='{CONTROL_NAME_SECRET}' "
            f"class='{CONTROL_CLASS_SECRET}' required maxlength='80' "
            f"value='{CURRENT_VALUE_SECRET}' autocomplete='name'>"
            "<label>Email address <input type='email' name='email' required "
            f"value='{escape(planted_email)}'></label>"
            "<label>Phone <input type='tel' name='phone' value=''></label>"
            "<label>Years of experience <input type='number' name='years' value='7'></label>"
            "<label for='note'>Cover note</label>"
            f"<textarea id='note' name='note' maxlength='500'>{CURRENT_VALUE_SECRET} note</textarea>"
            "<label for='country'>Country</label>"
            "<select id='country' name='country' required>"
            f"<option value='{OPTION_VALUE_SECRET}' selected>India</option>"
            "<option value='us-1'>United States</option>"
            "<option value='de-1'>Germany</option>"
            "</select>"
            "<fieldset><legend>Preferred contact</legend>"
            "<label><input type='radio' name='contact' value='CONTACT_VALUE_SECRET' checked> Email</label>"
            "<label><input type='radio' name='contact' value='phone'> Phone</label>"
            "</fieldset>"
            "<label><input type='checkbox' name='terms' checked> I agree to the terms</label>"
            f"<input type='text' name='referral' aria-label='Referral {FORM_FIELD_SECRET_MARKER}'>"
            "<label>Reference code <input type='text' name='ref' readonly value='R-1'></label>"
            "<label>Employee number <input type='text' name='emp' disabled value='E-9'></label>"
            f"<label>Reply address {planted_email} and account {planted_long_id}"
            " <input type='text' name='reply'></label>"
            f"<label>{FILE_LABEL_SECRET} <input type='file' name='resume'></label>"
            "<button type='button'>Save draft</button>"
            "<input type='submit' value='Continue'>"
            "<input type='submit' value='Hidden continue' "
            "style='position:absolute;width:1px;height:1px;clip:rect(0,0,0,0);overflow:hidden'>"
            "</form>"
            "<iframe title='same' src='/app/apply/frame'></iframe>"
            f"{cross}{EVENT_PROBE}",
        )

    @router.get("/app/apply/frame")
    async def app_apply_frame() -> Response:
        """A same-origin frame with controls of its own."""
        count("/app/apply/frame")
        return page(
            "Frame",
            f"<form><label>{FRAME_FIELD_LABEL} <input type='text' name='employer' "
            f"value='{CURRENT_VALUE_SECRET}_FRAME'></label>"
            "<label>Frame notes <textarea name='fnotes'></textarea></label></form>",
        )

    @router.get("/app/apply/frame-secure")
    async def app_apply_frame_secure() -> Response:
        """A same-origin frame that carries every credential-shaped control."""
        count("/app/apply/frame-secure")
        return page(
            "Secure frame",
            f"<form><label>{PASSWORD_LABEL_SECRET} <input type='password' name='pw'></label>"
            f"<label>{OTP_LABEL_SECRET} <input type='text' autocomplete='one-time-code' name='c'></label>"
            "<label>New <input type='text' autocomplete='new-password' name='n'></label>"
            "<label>Current <input type='text' autocomplete='current-password' name='cp'></label>"
            f"<label>{SECURE_FRAME_SIBLING_SECRET} sibling <input type='text' name='sib'></label>"
            "</form>",
        )

    @router.get("/app/apply/secure")
    async def app_apply_secure(request: Request) -> Response:
        """A main document that is fine, embedding a frame that is not."""
        count("/app/apply/secure")
        return account_page(
            request,
            "Apply secure",
            "<h1>Secure</h1><form><label>Visible field <input type='text' name='v'></label></form>"
            "<iframe title='secure' src='/app/apply/frame-secure'></iframe>",
        )

    @router.get("/app/apply/react")
    async def app_apply_react(request: Request) -> Response:
        """A form that re-renders without navigating, in controls that have no <form>.

        `window.lumiRerender(mode)` is the test hook: `changed` swaps the whole form
        for a different one, anything else rebuilds the original. The document
        never navigates, so the document epoch cannot move.
        """
        count("/app/apply/react")
        return account_page(
            request,
            "Apply react",
            "<h1>Application</h1><div id='root'></div>" + EVENT_PROBE + _REACT_SCRIPT,
        )

    @router.get("/app/apply/big")
    async def app_apply_big(request: Request) -> Response:
        """More than every bound: 8 forms, 96 controls, a 30-option select, 6 frames."""
        count("/app/apply/big")
        forms = "".join(
            f"<form aria-label='Group {n}'>"
            + "".join(f"<label>Field {n}-{m} <input type='text'></label>" for m in range(1, 13))
            + "</form>"
            for n in range(1, 9)
        )
        options = "".join(f"<option value='v{n}'>Choice {n}</option>" for n in range(1, 31))
        frames = "".join("<iframe title='f' src='/app/apply/frame'></iframe>" for _ in range(6))
        return account_page(
            request,
            "Apply big",
            f"<h1>Big</h1><form><label>Many <select>{options}</select></label></form>{forms}{frames}",
        )

    @router.post("/app/apply/submit")
    async def app_apply_submit() -> Response:
        """A submission Lumi may never make. The tests assert its counter stays 0."""
        effects["mutations"] += 1
        effects["submissions"] += 1
        return Response(status_code=204)

    @router.get("/xframe")
    async def cross_origin_frame() -> Response:
        """Served by the *other* fixture instance, so a frame on it is cross-origin."""
        count("/xframe")
        return page(
            "Cross frame",
            f"<form><label>{CROSS_ORIGIN_FIELD_SECRET} <input type='text' name='x'></label></form>",
        )


__all__: list[Any] = [
    "CONTROL_CLASS_SECRET",
    "CONTROL_ID_SECRET",
    "CONTROL_NAME_SECRET",
    "CROSS_ORIGIN_FIELD_SECRET",
    "CURRENT_VALUE_SECRET",
    "EVENT_PROBE",
    "FILE_LABEL_SECRET",
    "FORM_FIELD_SECRET_MARKER",
    "FRAME_FIELD_LABEL",
    "SECURE_FRAME_SIBLING_SECRET",
    "OPTION_VALUE_SECRET",
    "OTP_LABEL_SECRET",
    "PASSWORD_LABEL_SECRET",
    "add_form_routes",
]
