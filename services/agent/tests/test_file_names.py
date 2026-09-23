"""Milestone 10 S1: a relative name is refused before it can reach the filesystem."""

import pytest

from app.files.names import (
    FileNameRefusal,
    comparison_key,
    validate_file_name,
    validate_relative_path,
)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("..", "traversal"),
        ("a/../b.pdf", "traversal"),
        ("./a.pdf", "traversal"),
        ("a\\..\\..\\secret.txt", "traversal"),
        ("C:\\Windows\\win.ini", "absolute_path"),
        ("C:secret.txt", "absolute_path"),
        ("c:/x.pdf", "absolute_path"),
        ("/etc/passwd", "absolute_path"),
        ("\\secret.txt", "absolute_path"),
        ("\\\\server\\share\\a.pdf", "device_or_unc_path"),
        ("//server/share/a.pdf", "device_or_unc_path"),
        ("\\\\?\\C:\\a.pdf", "device_or_unc_path"),
        ("\\\\.\\PhysicalDrive0", "device_or_unc_path"),
        ("x/GLOBALROOT/Device", "device_or_unc_path"),
        ("resume.pdf:hidden", "alternate_stream_or_drive"),
        ("resume.pdf::$DATA", "alternate_stream_or_drive"),
        ("CON", "reserved_name"),
        ("con.pdf", "reserved_name"),
        ("docs/NUL .txt", "reserved_name"),
        ("LPT1.docx", "reserved_name"),
        ("COM\u00b9.txt", "reserved_name"),
        ("CONIN$", "reserved_name"),
        ("resume.pdf.", "trailing_dot_or_space"),
        ("resume.pdf ", "trailing_dot_or_space"),
        ("folder./a.pdf", "trailing_dot_or_space"),
        (" leading.pdf", "trailing_dot_or_space"),
        ("a<b.pdf", "invalid_character"),
        ("a|b.pdf", "invalid_character"),
        ("a*.pdf", "invalid_character"),
        ("a\x00b.pdf", "control_character"),
        ("a\nb.pdf", "control_character"),
        ("a\u202eb.pdf", "invalid_character"),
        ("cafe\u0301.pdf", "not_normalized"),
        ("a//b.pdf", "empty_component"),
        ("", "length"),
        ("x" * 600, "length"),
        ("/".join(["d"] * 20), "too_deep"),
    ],
)
def test_a_relative_path_that_is_not_plainly_under_the_root_is_refused(value: str, code: str) -> None:
    with pytest.raises(FileNameRefusal) as refused:
        validate_relative_path(value)
    assert refused.value.code == code
    assert value not in str(refused.value) or value == ""


@pytest.mark.parametrize("value", ["resume.pdf", "Jobs/2026/offer letter.docx", "notes.md", "a.b.c.txt", "caf\u00e9.pdf"])
def test_ordinary_relative_names_are_accepted(value: str) -> None:
    assert "/".join(validate_relative_path(value)) == value.replace("\\", "/")


def test_a_file_name_is_one_component() -> None:
    with pytest.raises(FileNameRefusal) as refused:
        validate_file_name("sub/resume.pdf")
    assert refused.value.code == "not_a_file_name"
    assert validate_file_name("resume.pdf") == "resume.pdf"


def test_names_compare_case_insensitively() -> None:
    assert comparison_key(("Docs", "Resume.PDF")) == comparison_key(("docs", "resume.pdf"))
