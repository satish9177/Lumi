"""The desktop worker's typed wire contract and the observation schema.

Two things to notice about every model here.

* **What is missing is the design.** There is no field for a window handle, a
  process id or path, a command line, a bounding rectangle or screen coordinate,
  an AutomationId, a ClassName, a FrameworkId, a RuntimeId, or a raw UIA property
  dictionary. `extra="forbid"` means a worker that tried to send one is refused
  by the parser rather than trusted. Those values may exist inside the worker so
  a future slice can re-derive a target; they do not cross this boundary.
* **The verbs are a closed, reviewed list.** Observation (list surfaces, observe one) and, from S3,
  exactly three effects (focus a surface, scroll a control by a closed step, open a registered
  application). No request names a selector, coordinates, a key, a path, a script or a property.

Everything here is `desktop_private` and `untrusted_environment`: text read from
another application is data about the world, never an instruction, and in this
slice it does not leave the local runtime.
"""

import re
import uuid
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

WORKER_TOKEN_HEADER: Final = "x-lumi-desktop-token"
READY_EVENT: Final = "lumi-desktop-worker-ready"

SCHEMA_VERSION: Final = 1
CLASSIFICATION: Final = "desktop_private"
TRUST: Final = "untrusted_environment"

# ---- bounds ----------------------------------------------------------------------

MAX_SURFACES: Final = 16
MAX_NODES: Final = 200
MAX_DEPTH: Final = 12
MAX_TEXT_PER_NODE: Final = 120
MAX_TOTAL_TEXT: Final = 12_288
#: How many elements a traversal may *look at* to decide whether the surface holds a
#: credential input. Larger than `MAX_NODES` on purpose: a password field that sits
#: past the projection cap still makes the whole surface a credential surface.
MAX_SCAN_ELEMENTS: Final = 1_000
MAX_SCAN_DEPTH: Final = 24
#: Most siblings read from any one parent. A list with twenty thousand rows must not be pulled across
#: the process boundary just to keep two hundred nodes of it; reaching the cap is declared as `scan`.
MAX_SIBLINGS: Final = 300
MAX_TITLE: Final = 120
MAX_APPLICATION_LABEL: Final = 64

SURFACE_REF_PATTERN: Final = re.compile(r"^s(?:[1-9]|1[0-6])$")
CONTROL_REF_PATTERN: Final = re.compile(r"^u(?:[1-9]\d?|1\d\d|200)$")


class DesktopRole(StrEnum):
    """A closed vocabulary. Anything UIA reports that is not here becomes `UNKNOWN`."""

    APP_BAR = "app_bar"
    BUTTON = "button"
    CALENDAR = "calendar"
    CHECK_BOX = "check_box"
    COMBO_BOX = "combo_box"
    CUSTOM = "custom"
    DATA_GRID = "data_grid"
    DATA_ITEM = "data_item"
    DOCUMENT = "document"
    EDIT = "edit"
    GROUP = "group"
    HEADER = "header"
    HEADER_ITEM = "header_item"
    HYPERLINK = "hyperlink"
    IMAGE = "image"
    LIST = "list"
    LIST_ITEM = "list_item"
    MENU = "menu"
    MENU_BAR = "menu_bar"
    MENU_ITEM = "menu_item"
    PANE = "pane"
    PROGRESS_BAR = "progress_bar"
    RADIO_BUTTON = "radio_button"
    SCROLL_BAR = "scroll_bar"
    SEMANTIC_ZOOM = "semantic_zoom"
    SEPARATOR = "separator"
    SLIDER = "slider"
    SPINNER = "spinner"
    SPLIT_BUTTON = "split_button"
    STATUS_BAR = "status_bar"
    TAB = "tab"
    TAB_ITEM = "tab_item"
    TABLE = "table"
    TEXT = "text"
    THUMB = "thumb"
    TITLE_BAR = "title_bar"
    TOOL_BAR = "tool_bar"
    TOOL_TIP = "tool_tip"
    TREE = "tree"
    TREE_ITEM = "tree_item"
    WINDOW = "window"
    UNKNOWN = "unknown"


#: UIA control-type name -> closed role. The keys are the strings pywinauto reports.
ROLE_BY_CONTROL_TYPE: Final[dict[str, DesktopRole]] = {
    "AppBar": DesktopRole.APP_BAR,
    "Button": DesktopRole.BUTTON,
    "Calendar": DesktopRole.CALENDAR,
    "CheckBox": DesktopRole.CHECK_BOX,
    "ComboBox": DesktopRole.COMBO_BOX,
    "Custom": DesktopRole.CUSTOM,
    "DataGrid": DesktopRole.DATA_GRID,
    "DataItem": DesktopRole.DATA_ITEM,
    "Document": DesktopRole.DOCUMENT,
    "Edit": DesktopRole.EDIT,
    "Group": DesktopRole.GROUP,
    "Header": DesktopRole.HEADER,
    "HeaderItem": DesktopRole.HEADER_ITEM,
    "Hyperlink": DesktopRole.HYPERLINK,
    "Image": DesktopRole.IMAGE,
    "List": DesktopRole.LIST,
    "ListItem": DesktopRole.LIST_ITEM,
    "Menu": DesktopRole.MENU,
    "MenuBar": DesktopRole.MENU_BAR,
    "MenuItem": DesktopRole.MENU_ITEM,
    "Pane": DesktopRole.PANE,
    "ProgressBar": DesktopRole.PROGRESS_BAR,
    "RadioButton": DesktopRole.RADIO_BUTTON,
    "ScrollBar": DesktopRole.SCROLL_BAR,
    "SemanticZoom": DesktopRole.SEMANTIC_ZOOM,
    "Separator": DesktopRole.SEPARATOR,
    "Slider": DesktopRole.SLIDER,
    "Spinner": DesktopRole.SPINNER,
    "SplitButton": DesktopRole.SPLIT_BUTTON,
    "StatusBar": DesktopRole.STATUS_BAR,
    "Tab": DesktopRole.TAB,
    "TabItem": DesktopRole.TAB_ITEM,
    "Table": DesktopRole.TABLE,
    "Text": DesktopRole.TEXT,
    "Thumb": DesktopRole.THUMB,
    "TitleBar": DesktopRole.TITLE_BAR,
    "ToolBar": DesktopRole.TOOL_BAR,
    "ToolTip": DesktopRole.TOOL_TIP,
    "Tree": DesktopRole.TREE,
    "TreeItem": DesktopRole.TREE_ITEM,
    "Window": DesktopRole.WINDOW,
}


class DesktopPattern(StrEnum):
    """Which UIA control patterns a control *advertises*.

    Availability only. Reading that a button has an invoke pattern is a fact about
    the tree; nothing in this slice calls one.
    """

    INVOKE = "invoke"
    VALUE = "value"
    TOGGLE = "toggle"
    SELECTION_ITEM = "selection_item"
    SELECTION = "selection"
    EXPAND_COLLAPSE = "expand_collapse"
    SCROLL = "scroll"
    TEXT = "text"
    RANGE_VALUE = "range_value"
    WINDOW = "window"


class CheckedState(StrEnum):
    ON = "on"
    OFF = "off"
    MIXED = "mixed"


class Truncation(StrEnum):
    NODES = "nodes"
    DEPTH = "depth"
    TEXT = "text"
    SCAN = "scan"
    #: The read ran out of its time budget. What is returned is real but incomplete, and says so.
    TIME = "time"


def clean_text(value: str) -> str:
    """Make any string the desktop hands us safe to encode.

    A window title or accessible name is whatever another program chose, including an unpaired
    UTF-16 surrogate that no UTF-8 encoder accepts. Left alone, one such title would fail model
    validation or the byte-budget encode, and the exception text would carry the string itself.
    """
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


SurfaceRef = Annotated[str, Field(pattern=SURFACE_REF_PATTERN.pattern)]
ControlRef = Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SurfaceRecord(_Wire):
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)
    window_title: str = Field(max_length=MAX_TITLE)
    visible: bool
    minimized: bool


class SurfaceListRequest(_Wire):
    expected_worker_generation: uuid.UUID


class SurfaceListResponse(_Wire):
    worker_generation: uuid.UUID
    surfaces: list[SurfaceRecord] = Field(max_length=MAX_SURFACES)
    #: More eligible surfaces existed than the inventory bound admits.
    truncated: bool


class ObserveRequest(_Wire):
    expected_worker_generation: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)


class DesktopNode(_Wire):
    control_ref: ControlRef
    parent_ref: ControlRef | None
    role: DesktopRole
    name: str | None = Field(max_length=MAX_TEXT_PER_NODE)
    text: str | None = Field(max_length=MAX_TEXT_PER_NODE)
    enabled: bool
    visible: bool
    focused: bool
    focusable: bool
    selected: bool | None
    checked: CheckedState | None
    expanded: bool | None
    patterns: list[DesktopPattern]


class DesktopObservation(_Wire):
    observation_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    worker_generation: uuid.UUID
    schema_version: Literal[1] = SCHEMA_VERSION
    classification: Literal["desktop_private"] = CLASSIFICATION
    trust: Literal["untrusted_environment"] = TRUST
    nodes: list[DesktopNode] = Field(max_length=MAX_NODES)
    node_count: int = Field(ge=0, le=MAX_NODES)
    depth: int = Field(ge=0, le=MAX_DEPTH)
    truncated: bool
    truncation: list[Truncation]
    #: Value-free structural digest: role, accessible name, patterns and enabled state
    #: in tree order. Text values, focus and check state are deliberately outside it.
    fingerprint: Sha256Hex


class WorkerIdentity(_Wire):
    worker_generation: uuid.UUID
    started_at: str
    platform: Literal["win32"]
    operations: list[str]


class WorkerErrorBody(_Wire):
    """Only a code. Never a title, a name, a process or any observed text."""

    code: str
    worker_generation: uuid.UUID | None = None


# ---- S3: three reviewed effects --------------------------------------------------
#
# The verbs the worker now has, and the only ones: bring one already-visible surface to the
# foreground, scroll one control through UIA's ScrollPattern by a closed step, and open one
# *registered* application. There is still no field for a handle, PID, path, argument, coordinate,
# key, selector or script. `dispatch_id` names the runtime's durable dispatch so the worker can
# refuse to perform one dispatch twice.

APP_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AppId = Annotated[str, Field(pattern=APP_ID_PATTERN.pattern)]
#: `GetLastInputInfo` tick. Only its identity matters (it changed or it did not); it is never a time.
InputTick = Annotated[int, Field(ge=0, le=0xFFFFFFFF)]


class ScrollStep(StrEnum):
    """The only scroll amounts that exist. A model never supplies a number."""

    SMALL_UP = "small_up"
    SMALL_DOWN = "small_down"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"


class InputBaselineRequest(_Wire):
    expected_worker_generation: uuid.UUID


class InputBaselineResponse(_Wire):
    worker_generation: uuid.UUID
    input_tick: InputTick


class FocusRequest(_Wire):
    expected_worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    #: The human-input baseline taken when the user approved. A newer input refuses the effect.
    input_tick: InputTick


class FocusResponse(_Wire):
    worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    outcome: Literal["focused", "not_focused"]
    #: The user typed or clicked while the effect ran. Automation stops; the runtime re-observes.
    input_changed: bool


class ScrollRequest(_Wire):
    expected_worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    control_ref: ControlRef
    step: ScrollStep
    input_tick: InputTick


class ScrollResponse(_Wire):
    worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    outcome: Literal["scrolled", "unchanged"]
    #: Vertical scroll position as a percentage, before and after (None when UIA reports none).
    percent_before: float | None = Field(ge=0, le=100)
    percent_after: float | None = Field(ge=0, le=100)
    input_changed: bool


class LaunchRequest(_Wire):
    expected_worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    app_id: AppId
    input_tick: InputTick


class LaunchResponse(_Wire):
    worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    app_id: AppId
    #: `already_running`: a live instance of exactly this registered executable existed, so no process
    #: was started and that instance was brought forward. `launched`: one process was started.
    outcome: Literal["launched", "already_running"]
    #: The registered instance's surface, when its window has appeared and is eligible.
    surface_ref: SurfaceRef | None
    surface_epoch: int | None = Field(ge=1)
    focused: bool
    input_changed: bool


# ---- S4: three bounded semantic mutations ----------------------------------------
#
# Exactly three more verbs, each on one control re-resolved from the current tree: set a value
# (`ValuePattern.SetValue`), select an item (`SelectionItem.Select`), invoke a control whose effect
# the controller already classified (`InvokePattern.Invoke`). No keyboard, mouse, pasted text, hotkey,
# drag or coordinate exists anywhere. The raw value a `SetValueRequest` carries is the one thing on
# this boundary that is not opaque: it is the trusted, user-supplied text to write, and it is never
# echoed back, logged or stored beyond this one request/response pair.

MAX_SET_VALUE_LENGTH: Final = 4_000


class InvokeEffect(StrEnum):
    """The closed, reviewed set of Invoke effects the worker will verify. One effect for now.

    `NAME_TOGGLE`: pressing the control is expected to change ITS OWN accessible name (a
    disclosure/expand/reveal-style button whose label reflects its own state, e.g. "Show details" /
    "Hide details"). Nothing about the button's *label text* is trusted; what is verified is that the
    control's semantic name differs, by exact re-resolved identity, from the name recorded when the
    action was proposed. A control whose name does not change is a known failure, not a guess.
    """

    NAME_TOGGLE = "name_toggle"


class SetValueRequest(_Wire):
    expected_worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    control_ref: ControlRef
    #: The trusted value to write. Never a model's text: the runtime places this here only from the
    #: durable, user-supplied value the plan approval named.
    value: str = Field(max_length=MAX_SET_VALUE_LENGTH)
    input_tick: InputTick


class SetValueResponse(_Wire):
    worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    #: `set`: the call returned and the re-read canonical value equals the approved value, exactly.
    #: `not_set`: the call returned and a re-read canonical value is available but does not match --
    #: a known, verified non-effect, not a guess. `uncertain`: the call returned but the verifying
    #: re-read itself did not produce a value (e.g. a transient COM failure) -- whether the write took
    #: is genuinely unknown, which is never treated the same as a known non-effect. Never carries the
    #: value itself either way.
    outcome: Literal["set", "not_set", "uncertain"]
    input_changed: bool


class SelectRequest(_Wire):
    expected_worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    #: The list/combo/tree the option must belong to, re-verified at effect time.
    container_ref: ControlRef
    option_ref: ControlRef
    input_tick: InputTick


class SelectResponse(_Wire):
    worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    #: `uncertain`: the verifying re-read of selected state itself did not produce an answer -- see
    #: `SetValueResponse.outcome`, the same distinction applies here.
    outcome: Literal["selected", "not_selected", "uncertain"]
    input_changed: bool


class InvokeRequest(_Wire):
    expected_worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    surface_ref: SurfaceRef
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    control_ref: ControlRef
    effect: InvokeEffect
    input_tick: InputTick


class InvokeResponse(_Wire):
    worker_generation: uuid.UUID
    dispatch_id: uuid.UUID
    outcome: Literal["invoked", "no_change"]
    input_changed: bool
