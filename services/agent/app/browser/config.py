"""Configuration for the browser worker process and for the runtime's client.

The worker's settings are read from its own environment, not from the agent's
`.env`. That is the point: the worker is the process that talks to untrusted web
pages, so it is the process that must not be able to read `DATABASE_URL` or any
provider key. Its entire configuration is a credential, a timeout, a headless
flag, and a list of origins it is permitted to visit.

The origin allowlist is the anti-SSRF boundary. A proposal names a *site* -- a
short reviewed name -- and the worker resolves that to a URL from its own
configuration. A proposal that carried a URL would let whoever wrote the
proposal choose where the browser goes.
"""

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.public_url import (
    BROKER_POLICY_VERSION,
    RESEARCH_POLICY_VERSION,
    PublicUrlPolicy,
    parse_allowed_hosts,
    parse_test_origins,
)

DEFAULT_SITE = "appointment_fixture"
#: The shape of a local fixture origin, mirrored from `parse_test_origins`.
_TEST_ORIGIN_SHAPE = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}")


def _parse_origins(raw: str) -> dict[str, str]:
    """`name=http://host:port,other=https://host` -> a mapping."""
    origins: dict[str, str] = {}
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        name, separator, origin = entry.partition("=")
        if not separator:
            raise ValueError(f"expected name=origin, got {entry!r}")
        origin = origin.rstrip("/")
        if not origin.startswith(("http://", "https://")):
            raise ValueError(f"origin for {name!r} must be an http(s) URL")
        origins[name.strip()] = origin
    return origins


class WorkerSettings(BaseSettings):
    """Read from the worker process's environment only. No `.env` file."""

    model_config = SettingsConfigDict(
        env_prefix="LUMI_BROWSER_", extra="ignore", hide_input_in_errors=True
    )

    #: Minted by trusted bootstrap code and handed over out of band.
    token: SecretStr
    #: `appointment_fixture=http://127.0.0.1:8801`
    allowed_origins: str = Field(default="")
    headless: bool = True
    #: Overrides every operation's declared timeout. Left at 0, each operation
    #: uses its own. Exists so an evaluation can widen the window deliberately.
    operation_timeout_seconds: float = Field(default=0.0, ge=0, le=3_600)
    #: Milestone 7a public inspection: hosts an approved inspection may open
    #: ("github.com,*.example.org"). Empty means no public inspection at all.
    public_hosts: str = Field(default="")
    #: Exact http://127.0.0.1:<port> origins of controlled test pages.
    inspection_test_origins: str = Field(default="")
    #: Milestone 7b public research. `research_any_public_host` replaces the
    #: host allowlist with "any name that is not local, private or reserved and
    #: resolves only to globally routable addresses" -- research cannot know its
    #: destinations in advance. `research_hosts` narrows it again when
    #: configuration wants a list, and the test origins exist for fixtures.
    #: All three empty/false means there is no research capability at all.
    research_any_public_host: bool = False
    research_hosts: str = Field(default="")
    research_test_origins: str = Field(default="")
    #: How many tabs one research session may own.
    research_max_tabs: int = Field(default=5, ge=1, le=5)

    @field_validator("research_hosts")
    @classmethod
    def _valid_research_hosts(cls, value: str) -> str:
        parse_allowed_hosts(value)
        return value

    @field_validator("research_test_origins")
    @classmethod
    def _valid_research_test_origins(cls, value: str) -> str:
        parse_test_origins(value)
        return value

    @field_validator("public_hosts")
    @classmethod
    def _valid_hosts(cls, value: str) -> str:
        parse_allowed_hosts(value)
        return value

    @field_validator("inspection_test_origins")
    @classmethod
    def _valid_test_origins(cls, value: str) -> str:
        parse_test_origins(value)
        return value

    @field_validator("token")
    @classmethod
    def _non_trivial(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("LUMI_BROWSER_TOKEN must be at least 16 characters")
        return value

    @property
    def origins(self) -> dict[str, str]:
        return _parse_origins(self.allowed_origins)

    @property
    def public_policy(self) -> PublicUrlPolicy:
        return PublicUrlPolicy(
            allowed_hosts=parse_allowed_hosts(self.public_hosts),
            test_origins=parse_test_origins(self.inspection_test_origins),
        )

    @property
    def broker_origins(self) -> frozenset[str]:
        """Local fixture origins the egress broker may dial without resolving.

        Exactly the `http://127.0.0.1:<port>` origins trusted configuration
        named -- the appointment fixture, the inspection fixture, the research
        fixture. They are the only destinations reachable over plaintext and the
        only ones that skip resolution, because there is no name to resolve and
        the address is already literal. Anything else, including a configured
        origin that is not loopback, takes the resolve-check-pin path.
        """
        candidates = {
            *self.origins.values(),
            *parse_test_origins(self.inspection_test_origins),
            *parse_test_origins(self.research_test_origins),
        }
        return frozenset(
            origin for origin in candidates if _TEST_ORIGIN_SHAPE.fullmatch(origin.rstrip("/"))
        )

    @property
    def broker_policy(self) -> PublicUrlPolicy:
        """The broker's own destination policy: shape and address, not scope.

        Scope -- *which* public host this task may read -- belongs to the
        runtime's grant and the context's network guard, and a CONNECT tunnel
        does not even carry a path to judge. The broker answers the narrower
        question of whether a socket may be opened at all, so it allows any host
        that passes layer 1 and resolves solely to globally routable addresses.
        """
        return PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True)

    @property
    def research_policy(self) -> PublicUrlPolicy:
        """The research destination policy. Never the inspection one: M7a keeps
        its narrow allowlist whatever research is configured to allow."""
        return PublicUrlPolicy(
            allowed_hosts=parse_allowed_hosts(self.research_hosts),
            test_origins=parse_test_origins(self.research_test_origins),
            version=RESEARCH_POLICY_VERSION,
            allow_any_public_host=self.research_any_public_host,
        )
