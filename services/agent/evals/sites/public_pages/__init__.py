"""Deterministic public-page fixtures for Milestone 7a page inspection.

Run with `uv run python -m evals.sites.public_pages.server --port 8811`.
A second instance on another port serves as a *canary*: a destination that is
never allowlisted, whose request log proves Lumi never contacted it.
"""

from evals.sites.public_pages.app import HOSTILE_INSTRUCTIONS, PROFILE, create_site

__all__ = ["HOSTILE_INSTRUCTIONS", "PROFILE", "create_site"]
