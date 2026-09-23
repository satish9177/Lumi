r"""Pure, filesystem-free rules for the names Lumi will look up under an approved root (Milestone 10 S1).

A relative path is checked here **before** anything touches the disk, so a hostile string can never
reach `os.open`. The rules are Windows' own naming rules, applied strictly, plus the ones that close
the classic confusions:

```text
refused                         why
..  .  empty component          traversal / aliasing
C:  X:\  \\server  //host       absolute, drive-relative or UNC: never relative to the root
\\?\  \\.\  GLOBALROOT          device namespace
name:stream  (any ':')          alternate data stream (or a drive letter)
CON PRN AUX NUL COM1 LPT1 ...   reserved device names, with or without an extension
trailing '.' or ' '             Windows strips them, so two spellings would name one file
< > " | ? * and controls        not a valid file name
non-NFC Unicode                 two byte strings that render (and may resolve) alike
```

A name that passes is still not authority: the broker resolves it under the root, refuses any reparse
point on the way down, and proves from the *open handle* that it opened the file it meant to.
"""

import re
import unicodedata
from typing import Final

MAX_RELATIVE_CHARS: Final = 512
MAX_COMPONENT_CHARS: Final = 255
MAX_DEPTH: Final = 16

_FORBIDDEN_CHARS: Final = frozenset('<>:"|?*')
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED: Final = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$", "clock$"}
    | {f"com{digit}" for digit in "0123456789\u00b9\u00b2\u00b3"}
    | {f"lpt{digit}" for digit in "0123456789\u00b9\u00b2\u00b3"}
)


class FileNameRefusal(ValueError):
    """A refused name. `code` is stable and never contains the name itself."""

    def __init__(self, code: str) -> None:
        super().__init__(f"That file name was refused ({code}).")
        self.code = code


def _is_reserved(component: str) -> bool:
    # Windows treats `NUL.txt`, `nul .pdf` and `CON` alike: the device name is whatever precedes the
    # first dot, with trailing spaces ignored.
    stem = component.split(".", 1)[0].rstrip(" ").casefold()
    return stem in _RESERVED


def validate_component(component: str) -> str:
    """One path component (a file or directory name)."""
    if not isinstance(component, str) or component == "":
        raise FileNameRefusal("empty_component")
    if component in (".", ".."):
        raise FileNameRefusal("traversal")
    if len(component) > MAX_COMPONENT_CHARS:
        raise FileNameRefusal("component_too_long")
    if _CONTROL.search(component):
        raise FileNameRefusal("control_character")
    if ":" in component:
        raise FileNameRefusal("alternate_stream_or_drive")
    if any(character in _FORBIDDEN_CHARS for character in component):
        raise FileNameRefusal("invalid_character")
    if component.endswith((".", " ")) or component.startswith(" "):
        raise FileNameRefusal("trailing_dot_or_space")
    if _is_reserved(component):
        raise FileNameRefusal("reserved_name")
    if any(unicodedata.category(character) in ("Cf", "Cs", "Co", "Cn") for character in component):
        raise FileNameRefusal("invalid_character")
    if unicodedata.normalize("NFC", component) != component:
        raise FileNameRefusal("not_normalized")
    return component


def validate_relative_path(value: object) -> tuple[str, ...]:
    """A root-relative path, as components. Refuses anything that is not plainly *under* the root."""
    if not isinstance(value, str):
        raise FileNameRefusal("not_text")
    if value == "" or len(value) > MAX_RELATIVE_CHARS:
        raise FileNameRefusal("length")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise FileNameRefusal("invalid_character") from None
    if _CONTROL.search(value):
        raise FileNameRefusal("control_character")
    normalized = value.replace("\\", "/")
    if normalized.startswith("//"):
        # `\\server\share`, `\\?\C:\`, `\\.\PhysicalDrive0`, `//?/...`: never relative to anything.
        raise FileNameRefusal("device_or_unc_path")
    if normalized.startswith("/"):
        raise FileNameRefusal("absolute_path")
    if re.match(r"^[A-Za-z]:", normalized):
        raise FileNameRefusal("absolute_path")
    if "globalroot" in normalized.casefold():
        raise FileNameRefusal("device_or_unc_path")
    components = tuple(normalized.split("/"))
    if len(components) > MAX_DEPTH:
        raise FileNameRefusal("too_deep")
    return tuple(validate_component(component) for component in components)


def validate_file_name(value: object) -> str:
    """A single file name, e.g. the destination name of a placed download."""
    if not isinstance(value, str):
        raise FileNameRefusal("not_text")
    if "/" in value or "\\" in value:
        raise FileNameRefusal("not_a_file_name")
    return validate_component(value)


def display_relative(components: tuple[str, ...]) -> str:
    """The root-relative display form. Never an absolute path."""
    return "/".join(components)


def comparison_key(components: tuple[str, ...]) -> str:
    """Windows compares names case-insensitively; two spellings of one name are one name."""
    return "/".join(component.casefold() for component in components)
