"""Authenticated form/control observation (Milestone 8b S4), inside the worker.

**S4 observes form structure only. It cannot type, choose, check, click, upload
or submit anything.** This module contains no call that changes a page: it lists
controls with one fixed, static, assignment-free DOM helper, and re-derives one
control by index to scroll it into view. Everything that would *change* a control
is absent by construction, and `tests/test_form_observation_source.py` fails the
build if a call-shaped mutation appears here.

What leaves this module is the value-free projection in
`app.domain.authenticated_forms`. What never leaves it:

* **The current value of any field.** The helper computes "empty or filled" for a
  text-like control *inside the page* and returns that one word. The value string
  is never returned, so it never crosses into Python at all.
* **Raw option values.** Only an `<option>`'s label is read; the `value` attribute
  is not touched.
* **Any DOM identity** -- id, name, class, tag, selector, path, HTML, geometry --
  and **frame URLs**. A frame is a slot number.
* **The locator description** (`ElementLocator`): a frame slot, a form key, an
  ordinal and the expected semantic identity. It lives in `AuthenticatedTab`
  memory, holds no value, is never persisted and never returned.

Exclusions happen **while listing**, not by filtering a finished inventory: a
password, file, one-time-code, current/new-password or WebAuthn control is never
recorded, and a frame that contains any credential-shaped control contributes
nothing at all. A page the S2 credential detector flags never reaches this code.

**Same-origin frames only.** A frame is inventoried only if it and every
ancestor are http(s) frames of the top-level document's origin. A cross-origin
frame is never evaluated, so not even its labels are read.

No `ElementHandle` survives a step. `resolve_element` re-lists the frame, requires
the same control count, the same ordinal and the same semantic identity, and
returns a handle that the caller uses once and disposes. No nearest match, no
fuzzy fallback, no forcing.
"""

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import ElementHandle, Frame, Page
from playwright.async_api import Error as PlaywrightError

from app.domain.authenticated_forms import (
    MAX_ELEMENTS,
    MAX_FORMS,
    MAX_FRAMES,
    MAX_NAME_CHARS,
    MAX_OPTIONS,
    VALUE_STATE_CONTROLS,
    ElementProjection,
    FormInventory,
    FormProjection,
    FrameProjection,
    OptionProjection,
)
from app.domain.redaction import Redactor

#: Bounded, per frame. A page with more controls than this is truncated.
MAX_FRAME_RECORDS = 100
_UNOWNED = "u"

#: The one DOM helper. **Static source**: it is never built from page text, model
#: text or any argument; the request object is data it reads, never code.
#: Observation only -- it assigns nothing, dispatches nothing, focuses nothing,
#: submits nothing, and it reads a field's value in exactly one place
#: (`hasValue`), which returns a boolean and discards the value.
#: `tests/test_form_observation_source.py` scans this text.
_HELPER = r"""
(request) => {
  const MAX_SCAN = 400, MAX_RECORDS = __MAX_RECORDS__, MAX_OPTIONS = __MAX_OPTIONS__, MAX_TEXT = 400;
  const doc = document;
  const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
  const bound = (text) => (text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) : text);
  const SKIP = new Set(['INPUT', 'SELECT', 'TEXTAREA', 'SCRIPT', 'STYLE', 'OPTION']);

  // Text of a subtree, never descending into a form control: a label that wraps
  // an input must not become a way to read what is typed into it.
  const textOf = (node, depth) => {
    if (depth === 0 && (node.tagName === 'INPUT' || node.tagName === 'SELECT' || node.tagName === 'TEXTAREA')) return '';
    if (depth > 6) return '';
    let out = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3) out += child.nodeValue;
      else if (child.nodeType === 1 && !SKIP.has(child.tagName)) out += ' ' + textOf(child, depth + 1) + ' ';
    }
    return out;
  };
  const byIds = (ids) => clean(ids.split(/\s+/).filter(Boolean)
    .map((id) => doc.getElementById(id)).filter(Boolean).map((n) => textOf(n, 0)).join(' '));
  const attr = (el, name) => el.getAttribute(name);
  const kind = (el) => (el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : '');

  const tokens = (el) => (attr(el, 'autocomplete') || '').toLowerCase().split(/\s+/);
  const SECRET = ['current-password', 'new-password', 'one-time-code', 'webauthn'];
  const isCredential = (el) =>
    kind(el) === 'password' || SECRET.some((t) => tokens(el).includes(t)) ||
    (el.tagName === 'INPUT' && /otp/i.test(attr(el, 'name') || ''));
  const isFile = (el) => kind(el) === 'file';

  // The single place a value is read. It answers a yes/no question and the string
  // goes no further.
  const hasValue = (el) => el.value.length > 0 || (el.validity ? el.validity.badInput === true : false);

  const isVisible = (el) => {
    try {
      if (typeof el.checkVisibility === 'function' &&
          !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    } catch (e) { /* older engines fall through to the geometry test */ }
    const rects = el.getClientRects();
    if (rects.length === 0) return false;
    if (rects[0].width <= 1 && rects[0].height <= 1) return false;
    // The "visually hidden" pattern: clipped to nothing while still laid out.
    const style = getComputedStyle(el);
    if (/^rect\(\s*0(px)?[ ,]+0(px)?[ ,]+0(px)?[ ,]+0(px)?\s*\)$/.test(style.clip || '')) return false;
    return !/inset\(\s*(50|100)%/.test(style.clipPath || '');
  };

  const groupLabel = (el) => {
    const legend = el.closest('fieldset') ? el.closest('fieldset').querySelector('legend') : null;
    return legend ? clean(textOf(legend, 0)) : '';
  };
  const nameOf = (el) => {
    const ids = attr(el, 'aria-labelledby');
    if (ids) { const named = byIds(ids); if (named) return named; }
    const aria = clean(attr(el, 'aria-label'));
    if (aria) return aria;
    if (el.labels && el.labels.length) {
      const named = clean(Array.from(el.labels).map((l) => textOf(l, 0)).join(' '));
      if (named) return named;
    }
    const type = kind(el);
    if (type === 'submit' || type === 'button' || type === 'reset') return clean(attr(el, 'value'));
    if (type === 'image') return clean(attr(el, 'alt'));
    if (el.tagName === 'BUTTON' || attr(el, 'role') === 'button') return clean(textOf(el, 1));
    const title = clean(attr(el, 'title'));
    if (title) return title;
    return clean(attr(el, 'placeholder'));
  };

  const SELECTOR = 'input, select, textarea, button, [role=textbox], [role=combobox], [role=listbox], ' +
    '[role=checkbox], [role=radiogroup], [role=button]';
  const matches = Array.from(doc.querySelectorAll(SELECTOR)).slice(0, MAX_SCAN);
  const credential = Array.from(doc.querySelectorAll('input, textarea, [autocomplete]')).some(isCredential);
  if (credential && request.mode === 'list') return { credential: true, more: false, records: [] };
  if (credential) return null;

  const forms = Array.from(doc.forms);
  const roleForms = Array.from(doc.querySelectorAll('[role=form]'));
  const formKeyOf = (el) => {
    const owner = el.form || null;
    if (owner) return 'f' + forms.indexOf(owner);
    const grouped = el.closest('[role=form]');
    return grouped ? 'r' + roleForms.indexOf(grouped) : '__UNOWNED__';
  };
  const formLabelOf = (key) => {
    let node = null;
    if (key[0] === 'f') node = forms[Number(key.slice(1))];
    else if (key[0] === 'r') node = roleForms[Number(key.slice(1))];
    if (!node) return '';
    const ids = attr(node, 'aria-labelledby');
    return (ids && byIds(ids)) || clean(attr(node, 'aria-label'));
  };

  const records = [];
  const elements = [];
  const seenRadioGroups = new Set();
  let more = false;
  let unnamedRadios = 0;
  for (const el of matches) {
    if (isCredential(el) || isFile(el)) continue;
    const type = kind(el);
    if (type === 'hidden') continue;
    const role = attr(el, 'role');
    if (el.tagName === 'INPUT' && type === 'radio' && el.closest('[role=radiogroup]')) continue;
    let out = null;
    const base = {
      formKey: formKeyOf(el), name: bound(nameOf(el)), valueState: 'unknown', required: false,
      enabled: !(el.matches(':disabled') || attr(el, 'aria-disabled') === 'true'),
      visible: isVisible(el), readOnly: false, maxLength: null, options: [], optionsTotal: 0, submitLike: false,
    };
    const flags = (target) => {
      target.required = el.required === true || attr(el, 'aria-required') === 'true';
      target.readOnly = el.readOnly === true || attr(el, 'aria-readonly') === 'true';
      return target;
    };
    const optionLabels = (list, labelOf) => {
      const all = Array.from(list);
      return { options: all.slice(0, MAX_OPTIONS).map((o) => bound(clean(labelOf(o)))), optionsTotal: all.length };
    };
    if (el.tagName === 'INPUT') {
      if (type === 'checkbox') {
        out = flags({ ...base, role: 'checkbox', controlType: 'checkbox' });
      } else if (type === 'radio') {
        const name = attr(el, 'name') || '';
        const key = (el.form ? 'f' + forms.indexOf(el.form) : '__UNOWNED__') + '|' + (name || 'n' + (unnamedRadios++));
        if (seenRadioGroups.has(key)) continue;
        seenRadioGroups.add(key);
        const members = Array.from(doc.querySelectorAll('input[type=radio]')).filter(
          (r) => name !== '' && attr(r, 'name') === name && (r.form || null) === (el.form || null));
        const group = members.length ? members : [el];
        out = flags({ ...base, role: 'radiogroup', controlType: 'radiogroup',
          name: bound(groupLabel(el) || base.name), ...optionLabels(group, nameOf) });
        out.required = group.some((r) => r.required === true);
      } else if (type === 'submit' || type === 'image') {
        out = { ...base, role: 'button', controlType: 'submit_like', submitLike: true };
      } else if (type === 'button' || type === 'reset') {
        out = { ...base, role: 'button', controlType: 'other' };
      } else {
        const known = { text: 'text', email: 'email', tel: 'tel', number: 'number' };
        const controlType = known[type] || 'other';
        out = flags({ ...base, role: 'textbox', controlType });
        if (known[type]) out.valueState = hasValue(el) ? 'filled' : 'empty';
        if (el.maxLength >= 0) out.maxLength = Math.min(el.maxLength, 1000000);
      }
    } else if (el.tagName === 'TEXTAREA') {
      out = flags({ ...base, role: 'textbox', controlType: 'textarea', valueState: hasValue(el) ? 'filled' : 'empty' });
      if (el.maxLength >= 0) out.maxLength = Math.min(el.maxLength, 1000000);
    } else if (el.tagName === 'SELECT') {
      const multiple = el.multiple || el.size > 1;
      out = flags({ ...base, role: multiple ? 'listbox' : 'combobox',
        controlType: multiple ? 'select_multi' : 'select_single', ...optionLabels(el.options, (o) => o.label) });
    } else if (el.tagName === 'BUTTON') {
      const submit = el.type === 'submit';
      out = { ...base, role: 'button', controlType: submit ? 'submit_like' : 'other', submitLike: submit };
    } else if (role === 'textbox') {
      out = flags({ ...base, role: 'textbox', controlType: 'other' });
    } else if (role === 'checkbox') {
      out = flags({ ...base, role: 'checkbox', controlType: 'checkbox' });
    } else if (role === 'button') {
      out = { ...base, role: 'button', controlType: 'other' };
    } else if (role === 'radiogroup') {
      out = flags({ ...base, role: 'radiogroup', controlType: 'radiogroup',
        ...optionLabels(el.querySelectorAll('[role=radio], input[type=radio]'), nameOf) });
    } else if (role === 'combobox' || role === 'listbox') {
      const multiple = attr(el, 'aria-multiselectable') === 'true';
      out = flags({ ...base, role: multiple ? 'listbox' : role,
        controlType: multiple ? 'select_multi' : 'select_single',
        ...optionLabels(el.querySelectorAll('[role=option]'), (o) => textOf(o, 1)) });
    }
    if (out === null) continue;
    if (records.length >= MAX_RECORDS) { more = true; break; }
    out.identity = JSON.stringify([out.role, out.controlType, out.name, out.required, out.readOnly, out.enabled, out.visible]);
    out.formLabel = formLabelOf(out.formKey);
    records.push(out);
    elements.push(el);
  }

  if (request.mode === 'list') return { credential: false, more, records };
  // resolve: the same enumeration must yield the same count, and the control at the
  // ordinal must have the same form and semantic identity. Otherwise: nothing.
  if (elements.length !== request.count || more) return null;
  const record = records[request.ordinal];
  if (!record || record.identity !== request.identity || record.formKey !== request.formKey) return null;
  return elements[request.ordinal];
}
""".replace("__MAX_RECORDS__", str(MAX_FRAME_RECORDS)).replace(
    "__MAX_OPTIONS__", str(MAX_OPTIONS)
).replace("__UNOWNED__", _UNOWNED)


@dataclass(frozen=True, slots=True)
class ElementLocator:
    """Worker-only description of one control. Never persisted, never returned.

    Holds no value and no DOM handle. It is enough to *re-derive* a control from
    the current DOM and to refuse if what is found is not the control that was
    observed.
    """

    frame_slot: int
    form_key: str
    ordinal: int
    frame_record_count: int
    identity: str
    document_epoch: int = 0
    form_epoch: int = 0


@dataclass(frozen=True, slots=True)
class CollectedInventory:
    """A finished listing: the projection, the worker-only locators, a fingerprint."""

    inventory: FormInventory
    #: Element ref -> locator, epochs not yet assigned (the tab assigns them).
    locators: dict[str, ElementLocator]
    #: A hash of the semantic structure. Worker-internal: never persisted, never sent.
    fingerprint: str


def _origin(url: str) -> tuple[str, str, int] | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return parts.scheme, parts.hostname, port or (443 if parts.scheme == "https" else 80)


def eligible_frames(page: Page) -> list[Frame]:
    """The main frame first, then same-origin descendants: at most `MAX_FRAMES`.

    A frame counts only if it **and every ancestor** share the top-level
    document's origin. Nothing is evaluated in any other frame.
    """
    main = page.main_frame
    main_origin = _origin(main.url)
    if main_origin is None:
        return []
    frames = [main]
    for frame in page.frames:
        if frame == main or len(frames) >= MAX_FRAMES:
            continue
        try:
            if frame.is_detached():
                continue
            ancestor: Frame | None = frame
            same_origin = True
            while ancestor is not None:
                if _origin(ancestor.url) != main_origin:
                    same_origin = False
                    break
                ancestor = ancestor.parent_frame
        except PlaywrightError:  # pragma: no cover - the frame went away.
            continue
        if same_origin:
            frames.append(frame)
    return frames


def _text(raw: object, redactor: Redactor) -> str:
    cleaned = " ".join(str(raw or "").replace("\x00", " ").split())
    return redactor.redact(cleaned)[:MAX_NAME_CHARS]


async def build_inventory(page: Page) -> CollectedInventory:
    """List the page's controls as a bounded, redacted, value-free inventory.

    Passive: nothing is focused, hovered, clicked, typed into or dispatched, and
    no field's value is returned. A frame that cannot be read is skipped.
    """
    redactor = Redactor()
    forms: list[FormProjection] = []
    form_refs: dict[tuple[int | None, str], str] = {}
    frames: list[FrameProjection] = []
    elements: list[ElementProjection] = []
    locators: dict[str, ElementLocator] = {}
    structure: list[object] = []
    truncated = False

    for slot, frame in enumerate(eligible_frames(page)):
        try:
            listing = await frame.evaluate(_HELPER, {"mode": "list"})
        except PlaywrightError:
            continue
        if not isinstance(listing, dict) or listing.get("credential") is True:
            # A credential-shaped control anywhere in the frame: nothing from it.
            continue
        frame_ref = f"fr{len(frames)}"
        frames.append(FrameProjection(ref=frame_ref))
        records = listing.get("records", [])
        truncated = truncated or bool(listing.get("more"))
        for ordinal, record in enumerate(records):
            if len(elements) >= MAX_ELEMENTS:
                truncated = True
                break
            form_key = str(record["formKey"])
            scope = (None if form_key == _UNOWNED else slot, form_key)
            form_ref = form_refs.get(scope)
            if form_ref is None:
                if len(forms) >= MAX_FORMS:
                    truncated = True
                    continue
                form_ref = f"f{len(forms) + 1}"
                form_refs[scope] = form_ref
                label = _text(record.get("formLabel"), redactor)
                forms.append(FormProjection(ref=form_ref, label=label or None))
            role = str(record["role"])
            control_type = str(record["controlType"])
            options = [_text(label, redactor) for label in record.get("options", [])]
            truncated = truncated or int(record.get("optionsTotal", 0)) > len(options)
            ref = f"e{len(elements) + 1}"
            elements.append(
                ElementProjection(
                    element_ref=ref,
                    form_ref=form_ref,
                    frame_ref=frame_ref,
                    role=role,
                    control_type=control_type,
                    accessible_name=_text(record.get("name"), redactor),
                    value_state=(
                        record["valueState"] if control_type in VALUE_STATE_CONTROLS else "unknown"
                    ),
                    required=bool(record["required"]),
                    enabled=bool(record["enabled"]),
                    visible=bool(record["visible"]),
                    read_only=bool(record["readOnly"]),
                    max_length=record.get("maxLength"),
                    option_refs=[
                        OptionProjection(ref=f"op{index}", label=label)
                        for index, label in enumerate(options, start=1)
                    ],
                    submit_like=bool(record["submitLike"]),
                )
            )
            locators[ref] = ElementLocator(
                frame_slot=slot,
                form_key=form_key,
                ordinal=ordinal,
                frame_record_count=len(records),
                identity=str(record["identity"]),
            )
            # The fingerprint's input: structure only. No current value, ever --
            # `valueState` is deliberately left out so typing never bumps it.
            structure.append(
                [slot, form_key, record["identity"], record.get("options", []), record.get("maxLength")]
            )
    fingerprint = hashlib.sha256(
        json.dumps(structure, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CollectedInventory(
        inventory=FormInventory(forms=forms, frames=frames, elements=elements, truncated=truncated),
        locators=locators,
        fingerprint=fingerprint,
    )


async def resolve_element(page: Page, locator: ElementLocator) -> ElementHandle | None:
    """Re-derive the control from the *current* DOM, or return `None`.

    Exactly one control must sit at the ordinal, the frame must list the same
    number of controls, and the control's semantic identity must be the one
    observed. There is no nearest match and no fuzzy fallback. The handle lives
    for this one step: the caller uses it once and disposes it.
    """
    frames = eligible_frames(page)
    if locator.frame_slot >= len(frames):
        return None
    request = {
        "mode": "resolve",
        "ordinal": locator.ordinal,
        "count": locator.frame_record_count,
        "identity": locator.identity,
        "formKey": locator.form_key,
    }
    try:
        handle = await frames[locator.frame_slot].evaluate_handle(_HELPER, request)
    except PlaywrightError:
        return None
    element = handle.as_element()
    if element is None:
        await handle.dispose()
    return element


__all__ = [
    "MAX_FRAME_RECORDS",
    "CollectedInventory",
    "ElementLocator",
    "build_inventory",
    "eligible_frames",
    "resolve_element",
]
