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

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SITE = "appointment_fixture"


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

    @field_validator("token")
    @classmethod
    def _non_trivial(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("LUMI_BROWSER_TOKEN must be at least 16 characters")
        return value

    @property
    def origins(self) -> dict[str, str]:
        return _parse_origins(self.allowed_origins)
