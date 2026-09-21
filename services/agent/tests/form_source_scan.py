"""A source guard for form observation code (Milestone 8b S4).

**S4 observes form structure only.** This scanner is how that stays true: it
fails on any call-shaped API that could change, drive or submit a control, in the
Python that observes forms and in the one DOM helper that runs in the page.

* Python is scanned by parsing it (`ast`), so a docstring or a comment that says
  "click" or "type" is not a call and cannot fail the scan, and a real
  `locator.click()` cannot hide in one.
* The JavaScript helper is scanned as text after comments are stripped. It may
  assign only to two named local record objects it builds itself (`out`,
  `target`); an assignment to anything else -- a control's `value`, `checked`,
  `innerHTML` -- fails, as does any method that acts on a control.

The tests run the scanner against planted violations, so a scanner that quietly
stopped matching would fail its own test.
"""

import ast
import re

#: Call-shaped methods that change, drive or submit something. Any receiver.
FORBIDDEN_CALLS = frozenset(
    {
        "fill", "type", "press", "press_sequentially", "click", "dblclick", "tap", "check",
        "uncheck", "set_checked", "select_option", "select_text", "set_input_files", "drag_to",
        "drag_and_drop", "dispatch_event", "request_submit", "submit", "focus", "blur", "hover",
        "clear", "evaluate_on_selector", "eval_on_selector", "eval_on_selector_all",
        "evaluate_on_selector_all", "set_value", "invoke", "write",
    }
)
#: The locators through which a control could be *acted on* rather than read.
FORBIDDEN_ATTRIBUTES = frozenset({"keyboard", "mouse", "touchscreen"})


def find_python_mutations(source: str) -> list[str]:
    """Every call-shaped mutation (and every input-device attribute) in `source`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_CALLS:
                found.append(f"line {node.lineno}: .{node.func.attr}(")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append(f"line {node.lineno}: .{node.attr}")
    return found


_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
#: `receiver.property = value` (not `==`, `===`, `=>`), receiver being any name.
_ASSIGNMENT = re.compile(r"\b([A-Za-z_$][\w$]*)\s*(?:\.\s*[A-Za-z_$][\w$]*|\[[^\]]*\])\s*(?:[-+*/|&^]|\*\*|<<|>>>?)?=(?![=>])")
_ALLOWED_RECEIVERS = frozenset({"out", "target"})
_FORBIDDEN_JS = (
    r"\.\s*(?:click|focus|blur|submit|requestSubmit|reset|dispatchEvent|select|setSelectionRange"
    r"|setRangeText|scrollIntoView|scroll|scrollTo|showPicker|stepUp|stepDown|setAttribute"
    r"|removeAttribute|remove|append|appendChild|insertBefore|replaceWith|write|open|close"
    r"|setCustomValidity|reportValidity|checkValidity)\s*\(",
    r"\b(?:new\s+\w*Event|createEvent|Event\s*\()",
    r"\b(?:fetch|eval|XMLHttpRequest|sendBeacon|WebSocket|Function)\b",
    r"\b(?:innerHTML|outerHTML|innerText\s*=|textContent\s*=|document[.]cookie|local" "Storage|session" "Storage)\b",
    r"\+\+|--",
)
#: The one reviewed read of a control's value, in `hasValue`. It returns a boolean.
_VALUE_READS = re.compile(r"\.\s*value\b")
_REVIEWED_VALUE_READS = 1


def find_javascript_mutations(source: str) -> list[str]:
    """Every action, assignment or second value-read in the page-side helper."""
    code = _COMMENTS.sub(" ", source)
    found: list[str] = []
    for match in _ASSIGNMENT.finditer(code):
        if match.group(1) not in _ALLOWED_RECEIVERS:
            found.append(f"assigns to {match.group(0).strip()!r}")
    for pattern in _FORBIDDEN_JS:
        for match in re.finditer(pattern, code):
            # `++`/`--` is only a counter; a local counter is fine but must be reviewed.
            if match.group(0) in ("++", "--"):
                continue
            found.append(f"forbidden: {match.group(0).strip()!r}")
    reads = len(_VALUE_READS.findall(code))
    if reads != _REVIEWED_VALUE_READS:
        found.append(f"reads a value {reads} time(s); exactly {_REVIEWED_VALUE_READS} is reviewed")
    return found


# ---- Milestone 8b S6: the worker-wide scanner -------------------------------------------------

#: Call names that are only *actions* when their receiver is a page, a frame, a locator, a
#: handle or a control. `policy.check(url)`, `some_dict.clear()` and `writer.write(bytes)` are
#: not, and a scanner that flagged them would be a scanner nobody could keep green.
_RECEIVER_SENSITIVE = {
    "check": r"(locator|handle|member|control|page|frame|element)\b",
    "clear": r"(locator|handle|member|control|page|frame|element)\b",
    "write": r"(locator|handle|member|control|page|frame|keyboard)\b",
}
#: What `local_form_draft.py` alone may call: three write primitives, one of which (a checked
#: state) has two spellings.
S6_PRIMITIVES = frozenset({"fill", "select_option", "set_checked", "check"})


def find_worker_mutations(source: str, *, allow: frozenset[str] = frozenset()) -> list[str]:
    """Every Playwright-shaped action in `source`, except the names in `allow`.

    Like `find_python_mutations` (the S4 scanner), but receiver-aware for the three names
    that are ordinary methods elsewhere, so the whole worker tree can be scanned and the only
    thing that varies per file is which of the reviewed primitives it may contain.
    """
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            found.append(f"line {node.lineno}: .{node.attr}")
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = node.func.attr
        if name not in FORBIDDEN_CALLS or name in allow:
            continue
        pattern = _RECEIVER_SENSITIVE.get(name)
        if pattern is not None:
            receiver = ast.unparse(node.func.value).lower()
            if re.search(pattern, receiver.replace(".", " ").replace("_", " ")) is None:
                continue
        found.append(f"line {node.lineno}: .{name}(")
    return found
