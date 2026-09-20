"""A deterministic "site with sessions and a login flow", for Milestone 8a.

Run with `uv run python -m evals.sites.account_fixture.server --port 8821`.
S1 uses it to prove a persistent profile kept its session by asking the
*server* what it sees, never by exporting a cookie from the browser. S2 adds
a synthetic password/OTP/SSO/challenge login flow on the same fixture; see
`app.py` for the full route list and the fixture's own module docstring.
"""

from evals.sites.account_fixture.forms import (
    CONTROL_CLASS_SECRET,
    CONTROL_ID_SECRET,
    CONTROL_NAME_SECRET,
    CROSS_ORIGIN_FIELD_SECRET,
    CURRENT_VALUE_SECRET,
    FILE_LABEL_SECRET,
    FORM_FIELD_SECRET_MARKER,
    FRAME_FIELD_LABEL,
    SECURE_FRAME_SIBLING_SECRET,
    OPTION_VALUE_SECRET,
    OTP_LABEL_SECRET,
    PASSWORD_LABEL_SECRET,
)
from evals.sites.account_fixture.app import (
    ACCOUNT_ID,
    ACCOUNT_NAME,
    INJECTION_TEXT,
    PLANTED_CARD,
    PLANTED_EMAIL,
    PLANTED_LONG_ID,
    PLANTED_PHONE,
    PRIVATE_MARKER,
    PRIVATE_REPOSITORIES,
    REPOSITORIES,
    FIXTURE_OTP,
    FIXTURE_PASSWORD,
    FIXTURE_USERNAME,
    SECOND_ACCOUNT_ID,
    SECOND_ACCOUNT_NAME,
    SESSION_COOKIE,
    SIGNED_IN,
    SIGNED_OUT,
    SSO_TOKEN,
    create_site,
)

__all__ = [
    "CONTROL_CLASS_SECRET",
    "CONTROL_ID_SECRET",
    "CONTROL_NAME_SECRET",
    "CROSS_ORIGIN_FIELD_SECRET",
    "CURRENT_VALUE_SECRET",
    "FILE_LABEL_SECRET",
    "FORM_FIELD_SECRET_MARKER",
    "FRAME_FIELD_LABEL",
    "SECURE_FRAME_SIBLING_SECRET",
    "OPTION_VALUE_SECRET",
    "OTP_LABEL_SECRET",
    "PASSWORD_LABEL_SECRET",
    "ACCOUNT_ID",
    "ACCOUNT_NAME",
    "INJECTION_TEXT",
    "PLANTED_CARD",
    "PLANTED_EMAIL",
    "PLANTED_LONG_ID",
    "PLANTED_PHONE",
    "PRIVATE_MARKER",
    "PRIVATE_REPOSITORIES",
    "REPOSITORIES",
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
