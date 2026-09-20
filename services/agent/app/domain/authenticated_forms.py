"""Milestone 8b S4: the bounded, value-free shape of an authenticated form.

**S4 observes form structure only. It cannot type, choose, check, click, upload
or submit anything.** Nothing in this module can express a change: there is no
value, no option value, no selector and nothing to invoke. It exists so a later
slice can *talk about* a control (by an opaque ref) without ever having been
told what the control currently holds.

Refs are worker-generated and opaque, and a page or a model cannot choose what
one means:

```text
forms     f1 .. f5
frames    fr0 .. fr4     fr0 is always the main frame
elements  e1 .. e40
options   op1 .. op25    numbered per control
```

What an element projection carries, and -- more to the point -- what it never
carries:

```text
carries   role, control type, a redacted bounded accessible name, valueState
          (empty | filled | unknown), required, enabled, visible, readOnly,
          maxLength, bounded redacted option labels, submitLike
never     current or default value, value preview, option value, id, name,
          class, tag name, selector, XPath, DOM path, HTML, event handler,
          script, coordinates, bounding box, dataset, form action, form method,
          raw autocomplete, frame URL, or any locator description
```

`valueState` is the only value information there is. The worker reads whether a
text-like field is empty inside the page, keeps that one word, and the value
itself never reaches Python at all.

Every string here is untrusted page text. Names and labels leave the worker
only after S3's identifier reduction, and this model refuses one that would
still change under a fresh `Redactor` -- the same check `AuthenticatedObservation`
applies to blocks.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.redaction import is_redacted
from app.domain.research import BLOCK_REF, _plain

#: Reviewed maxima. Conservative, and documented in `docs/reviews/milestone-8-s4.md`.
MAX_FORMS = 5
MAX_FRAMES = 5
MAX_ELEMENTS = 40
MAX_OPTIONS = 25
MAX_NAME_CHARS = 120

FORM_REF = r"^f[1-5]$"
FRAME_REF = r"^fr[0-4]$"
ELEMENT_REF = r"^e([1-9]|[1-3][0-9]|40)$"
OPTION_REF = r"^op([1-9]|1[0-9]|2[0-5])$"

ElementRole = Literal[
    "textbox", "combobox", "listbox", "checkbox", "radiogroup", "button", "link", "option"
]
ControlType = Literal[
    "text",
    "email",
    "tel",
    "number",
    "textarea",
    "select_single",
    "select_multi",
    "checkbox",
    "radiogroup",
    "submit_like",
    "other",
]
ValueState = Literal["empty", "filled", "unknown"]

#: Only these can be described as empty or filled. Everything else is `unknown`:
#: the meaning of a "value" for a checkbox, a select or a widget is ambiguous, and
#: S4 does not guess.
VALUE_STATE_CONTROLS: frozenset[str] = frozenset({"text", "email", "tel", "number", "textarea"})


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OptionProjection(_Frozen):
    """One choice of a select or radio group: a ref and a redacted label. No value."""

    ref: str = Field(pattern=OPTION_REF)
    label: str = Field(max_length=MAX_NAME_CHARS)

    @field_validator("label")
    @classmethod
    def _label_is_plain(cls, value: str) -> str:
        return _plain(value)


class FormProjection(_Frozen):
    """A logical form group. No id, name, action, method or selector."""

    ref: str = Field(pattern=FORM_REF)
    #: A bounded, redacted label, only if the page offers one. Never fabricated.
    label: str | None = Field(default=None, max_length=MAX_NAME_CHARS)

    @field_validator("label")
    @classmethod
    def _label_is_plain(cls, value: str | None) -> str | None:
        return _plain(value) if value is not None else None


class FrameProjection(_Frozen):
    """A same-origin frame that was inventoried. No URL, ever."""

    ref: str = Field(pattern=FRAME_REF)


class ElementProjection(_Frozen):
    element_ref: str = Field(pattern=ELEMENT_REF)
    form_ref: str = Field(pattern=FORM_REF)
    frame_ref: str = Field(pattern=FRAME_REF)
    role: ElementRole
    control_type: ControlType
    accessible_name: str = Field(default="", max_length=MAX_NAME_CHARS)
    #: Reserved. S4 never sets it: a label cannot be tied to a text block without
    #: the fuzzy matching this slice refuses to do.
    label_ref: str | None = Field(default=None, pattern=BLOCK_REF)
    value_state: ValueState = "unknown"
    required: bool = False
    enabled: bool = True
    visible: bool = True
    read_only: bool = False
    max_length: int | None = Field(default=None, ge=0, le=1_000_000)
    option_refs: list[OptionProjection] = Field(default_factory=list, max_length=MAX_OPTIONS)
    #: Can only ever *remove* a future capability. `False` means "not recognised
    #: as a submit control", never "safe".
    submit_like: bool = False

    @field_validator("accessible_name")
    @classmethod
    def _name_is_plain(cls, value: str) -> str:
        return _plain(value)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.value_state != "unknown" and self.control_type not in VALUE_STATE_CONTROLS:
            raise ValueError("only text-like controls report empty or filled")
        if self.option_refs and self.control_type not in ("select_single", "select_multi", "radiogroup"):
            raise ValueError("only a select or a radio group carries options")
        if [option.ref for option in self.option_refs] != [
            f"op{index}" for index in range(1, len(self.option_refs) + 1)
        ]:
            raise ValueError("option refs must be sequential")
        if self.submit_like and self.role != "button":
            raise ValueError("only a button can be submit-like")
        if self.control_type == "submit_like" and not self.submit_like:
            raise ValueError("a submit_like control is submit-like")
        return self


class FormInventory(_Frozen):
    """The bounded inventory of one document. Untrusted, account-private, value-free."""

    forms: list[FormProjection] = Field(default_factory=list, max_length=MAX_FORMS)
    frames: list[FrameProjection] = Field(default_factory=list, max_length=MAX_FRAMES)
    elements: list[ElementProjection] = Field(default_factory=list, max_length=MAX_ELEMENTS)
    #: A bound (forms, frames, elements or options) cut something off.
    truncated: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if [form.ref for form in self.forms] != [f"f{i}" for i in range(1, len(self.forms) + 1)]:
            raise ValueError("form refs must be sequential")
        if [frame.ref for frame in self.frames] != [f"fr{i}" for i in range(len(self.frames))]:
            raise ValueError("frame refs must be sequential from the main frame")
        if [element.element_ref for element in self.elements] != [
            f"e{i}" for i in range(1, len(self.elements) + 1)
        ]:
            raise ValueError("element refs must be sequential")
        forms = {form.ref for form in self.forms}
        frames = {frame.ref for frame in self.frames}
        for element in self.elements:
            if element.form_ref not in forms or element.frame_ref not in frames:
                raise ValueError("an element names a form and a frame of this inventory")
        strings = [form.label for form in self.forms if form.label is not None]
        for element in self.elements:
            strings.append(element.accessible_name)
            strings.extend(option.label for option in element.option_refs)
        # The redaction guarantee is checked here as well as where it is applied.
        if not all(is_redacted(text) for text in strings):
            raise ValueError("the inventory still contains an unredacted identifier")
        return self

    @property
    def form_count(self) -> int:
        return len(self.forms)

    @property
    def element_count(self) -> int:
        return len(self.elements)

    @property
    def option_count(self) -> int:
        return sum(len(element.option_refs) for element in self.elements)


#: What a v1 observation and any observation without a form carries.
EMPTY_INVENTORY = FormInventory()

__all__ = [
    "ELEMENT_REF",
    "EMPTY_INVENTORY",
    "FORM_REF",
    "FRAME_REF",
    "MAX_ELEMENTS",
    "MAX_FORMS",
    "MAX_FRAMES",
    "MAX_NAME_CHARS",
    "MAX_OPTIONS",
    "OPTION_REF",
    "VALUE_STATE_CONTROLS",
    "ControlType",
    "ElementProjection",
    "ElementRole",
    "FormInventory",
    "FormProjection",
    "FrameProjection",
    "OptionProjection",
    "ValueState",
]
