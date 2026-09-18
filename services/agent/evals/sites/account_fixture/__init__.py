"""A deterministic "site with sessions", for Milestone 8a S1 profile persistence.

Run with `uv run python -m evals.sites.account_fixture.server --port 8821`.
It exists so a test can prove a persistent profile kept its session by asking
the *server* what it sees, never by exporting a cookie from the browser.
"""

from evals.sites.account_fixture.app import (
    ACCOUNT_NAME,
    SESSION_COOKIE,
    SIGNED_IN,
    SIGNED_OUT,
    create_site,
)

__all__ = ["ACCOUNT_NAME", "SESSION_COOKIE", "SIGNED_IN", "SIGNED_OUT", "create_site"]
