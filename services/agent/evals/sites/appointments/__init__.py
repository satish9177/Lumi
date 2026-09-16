"""Deterministic appointment-booking fixture site.

Run it with `uv run python -m evals.sites.appointments.server --port 8801`.
"""

from evals.sites.appointments.app import create_site
from evals.sites.appointments.state import SLOTS, SLOTS_BY_ID, AppointmentStore, Faults, Slot

__all__ = ["SLOTS", "SLOTS_BY_ID", "AppointmentStore", "Faults", "Slot", "create_site"]
