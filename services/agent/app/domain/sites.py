"""What Lumi is willing to believe about each reviewed site.

The question this file answers is narrow and load-bearing: **when a lookup says
"no booking exists", does that mean no booking exists?**

For most of the web the answer is no. A booking can be pending, queued, held
behind a payment that has not settled, visible only to a logged-in session, or
simply eventually consistent. On such a site, a lookup returning nothing is
evidence about the lookup, not about the world, and an action whose response was
lost has to stay `OUTCOME_UNKNOWN` until something better turns up. Reporting it
as `FAILED` would be Lumi claiming knowledge it does not have -- the exact
mistake the whole runtime is built to avoid.

The deterministic fixture is the rare case where the answer is yes, and it is
yes for reasons that are written down rather than assumed. Trusting absence is
therefore a per-site decision recorded here, next to its justification, instead
of a property of the reconciliation code.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SiteTrust:
    site: str
    #: May `NOT_FOUND` from this site's lookup be recorded as a known failure?
    lookup_absence_is_authoritative: bool
    #: Why. If this paragraph stops being true, the flag must change with it.
    rationale: str


APPOINTMENT_FIXTURE = SiteTrust(
    site="appointment_fixture",
    lookup_absence_is_authoritative=True,
    rationale=(
        "The fixture's booking store is the single writer, in one process, with no "
        "queue, no settlement step and no expiry. A booking is visible to the very "
        "next lookup by construction, and a booking that was never created can never "
        "appear later. Absence is therefore a fact about the world. This holds only "
        "because the site is a controlled fixture; it is not a claim about any real "
        "appointment website."
    ),
)

_SITES: dict[str, SiteTrust] = {APPOINTMENT_FIXTURE.site: APPOINTMENT_FIXTURE}


def site_trust(site: str) -> SiteTrust:
    """Unknown sites are trusted with nothing: absence proves nothing about them."""
    return _SITES.get(
        site,
        SiteTrust(
            site=site,
            lookup_absence_is_authoritative=False,
            rationale="No reviewed trust declaration exists for this site.",
        ),
    )
