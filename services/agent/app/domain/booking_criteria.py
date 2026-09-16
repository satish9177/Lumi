"""The constraints a booking task searches with.

A task's `request` is JSON the runtime keeps verbatim. This module is where the
booking constraints in it become a checked type, and where they are applied to
what the browser worker *observed* -- never to what a caller or a model claimed
a slot looks like.

Everything here is language-neutral: a weekday name from a closed set, 24-hour
`HH:MM` wall-clock bounds in the site's own offset, and an integer price ceiling
in an ISO currency. Whatever language the user spoke, the voice layer has to
produce these values before anything is searched.

`specialty` and `day` keep Milestone 4's lenient reading (an unreadable value is
treated as "any"), because the site filters on them itself. The newer bounds
fail closed: a stored time or price bound that does not parse makes the task
unsearchable rather than silently broader.
"""

import re
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.booking import BookingProposal

SPECIALTY_PATTERN = r"^[A-Za-z][A-Za-z .'-]{0,59}$"
TIME_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SPECIALTY = re.compile(SPECIALTY_PATTERN)

#: The request keys this module owns. Anything else in a request is not a
#: booking constraint and is left alone.
CRITERIA_KEYS = (
    "specialty",
    "day",
    "earliest_time",
    "latest_time",
    "max_price",
    "max_price_currency",
)


class InvalidBookingCriteriaError(ValueError):
    """Stored or supplied constraints that cannot be applied safely."""


class BookingCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specialty: str = Field(default="", max_length=60)
    day: str = Field(default="", max_length=9)
    earliest_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    latest_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    max_price: int | None = Field(default=None, ge=0, le=10_000_000, strict=True)
    max_price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.specialty and not _SPECIALTY.match(self.specialty):
            raise ValueError("specialty must be letters")
        if self.day and self.day not in DAYS:
            raise ValueError("day must be a weekday name")
        if (self.max_price is None) != (self.max_price_currency is None):
            raise ValueError("a price ceiling needs its currency, and only then")
        if (
            self.earliest_time is not None
            and self.latest_time is not None
            and self.earliest_time > self.latest_time
        ):
            raise ValueError("earliest_time must not be after latest_time")
        return self

    # ---- persistence ------------------------------------------------------

    @classmethod
    def from_request(cls, request: dict[str, Any]) -> "BookingCriteria":
        """Read the constraints stored in a task request."""
        specialty = request.get("specialty", "")
        day = request.get("day", "")
        if not isinstance(specialty, str) or (specialty and not _SPECIALTY.match(specialty)):
            specialty = ""
        if not isinstance(day, str) or (day and day not in DAYS):
            day = ""
        bounds = {
            key: request[key]
            for key in ("earliest_time", "latest_time", "max_price", "max_price_currency")
            if key in request
        }
        try:
            return cls.model_validate({"specialty": specialty, "day": day, **bounds})
        except ValidationError:
            raise InvalidBookingCriteriaError("stored booking criteria are unreadable") from None

    def request_fields(self) -> dict[str, Any]:
        """The request keys this criteria set writes. Unset bounds are omitted."""
        fields: dict[str, Any] = {}
        if self.specialty:
            fields["specialty"] = self.specialty
        if self.day:
            fields["day"] = self.day
        for key in ("earliest_time", "latest_time", "max_price", "max_price_currency"):
            value = getattr(self, key)
            if value is not None:
                fields[key] = value
        return fields

    def site_search(self) -> dict[str, str]:
        """The two fields the reviewed site's own search form accepts."""
        return {"specialty": self.specialty, "day": self.day}

    # ---- application ------------------------------------------------------

    def admits(self, *, time: datetime, price: int, currency: str) -> bool:
        """Whether one observed slot satisfies the time window and price ceiling.

        The wall-clock time is taken in the offset the site itself reported, so
        "after 18:00" means 18:00 at the clinic, not on the machine running Lumi.
        A slot priced in another currency never satisfies a ceiling: converting
        would be a guess.
        """
        if time.tzinfo is None:
            return False
        clock = f"{time.hour:02d}:{time.minute:02d}"
        if self.earliest_time is not None and clock < self.earliest_time:
            return False
        if self.latest_time is not None and clock > self.latest_time:
            return False
        if self.max_price is not None:
            if currency != self.max_price_currency or price > self.max_price:
                return False
        return True

    def admits_proposal(self, proposal: BookingProposal) -> bool:
        """Whether a prepared booking still satisfies these constraints.

        The proposal carries no specialty, so this is only meaningful together
        with `same_specialty`: a changed specialty invalidates every prepared
        booking.
        """
        # weekday() rather than strftime("%A"), which follows the process locale.
        if self.day and DAYS[proposal.time.weekday()] != self.day:
            return False
        return self.admits(time=proposal.time, price=proposal.price, currency=proposal.currency)

    def same_specialty(self, other: "BookingCriteria") -> bool:
        return self.specialty.casefold() == other.specialty.casefold()


def revised_request(request: dict[str, Any], criteria: BookingCriteria) -> dict[str, Any]:
    """The task request with its booking constraints replaced, everything else kept."""
    kept = {key: value for key, value in request.items() if key not in CRITERIA_KEYS}
    return {**kept, **criteria.request_fields()}
