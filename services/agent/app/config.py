import re
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.domain.public_url import PublicUrlPolicy, parse_allowed_hosts, parse_test_origins

AGENT_ROOT = Path(__file__).resolve().parents[1]


class DatabaseSettings(BaseSettings):
    """Only what a schema migration needs.

    Alembic runs outside any runtime process generation, so it must not require
    the per-process runtime credential.
    """

    # Anchored to services/agent so running from the repository root never reads
    # the Electron .env, which holds provider secrets this service must not see.
    model_config = SettingsConfigDict(
        env_file=AGENT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL is not a valid database URL") from None
        if url.drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        if not url.database:
            raise ValueError("DATABASE_URL must name a database")
        return value


class Settings(DatabaseSettings):
    # Minted afresh by Electron main for every process generation. It is
    # mandatory even in development and tests: there is no unauthenticated
    # mode whose accidental use could expose the action ledger on loopback.
    runtime_token: SecretStr = Field(validation_alias="LUMI_RUNTIME_TOKEN")
    # Electron supplies its own pid so the runtime can fail closed when the
    # desktop is hard-killed and therefore cannot run normal shutdown hooks.
    runtime_parent_pid: int | None = Field(
        default=None, validation_alias="LUMI_RUNTIME_PARENT_PID", ge=1
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # How long a granted approval may be claimed for execution. Checked by the
    # statement that claims it, not by a cleanup job, so an expired approval is
    # unusable the moment it expires even if nothing has swept it.
    approval_ttl_seconds: int = Field(default=300, ge=5, le=86_400)

    # --- browser worker ---------------------------------------------------
    #
    # All three are optional. With no worker configured the runtime behaves
    # exactly as it did in Milestone 2 and the browser routes answer 503, so a
    # deployment that has no business driving a browser cannot accidentally
    # acquire one by forgetting a flag.
    #
    # The token is minted by trusted bootstrap code and shared out of band with
    # the worker process. It is a SecretStr so it never reaches a log line, a
    # traceback or an error response, and it is sent as a request header --
    # never in a URL, which would put it in access logs and browser history.
    browser_worker_url: str | None = None
    browser_worker_token: SecretStr | None = None
    #: How long the runtime waits for one dispatch. Generous on purpose: cutting
    #: a consequential operation short does not undo it, it only converts a
    #: knowable outcome into an unknown one.
    browser_worker_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    #: Managed mode: when no external worker is configured, the runtime launches
    #: and owns its own worker, allowed to visit exactly this reviewed origin.
    #: Supplied by Electron main; loopback only in this milestone.
    browser_site_origin: str | None = Field(
        default=None, validation_alias="LUMI_BROWSER_SITE_ORIGIN"
    )
    browser_headless: bool = Field(default=True, validation_alias="LUMI_BROWSER_HEADLESS")
    #: Milestone 7a public page inspection. Hosts (`github.com,*.example.org`)
    #: an approved inspection may open, and exact loopback test origins. Both
    #: empty (the default) means there is no public inspection capability.
    #: Supplied by Electron main from its own trusted configuration.
    public_inspection_hosts: str = Field(default="", validation_alias="LUMI_PUBLIC_INSPECTION_HOSTS")
    inspection_test_origins: str = Field(default="", validation_alias="LUMI_INSPECTION_TEST_ORIGINS")

    @field_validator("public_inspection_hosts")
    @classmethod
    def _valid_inspection_hosts(cls, value: str) -> str:
        parse_allowed_hosts(value)
        return value

    @field_validator("inspection_test_origins")
    @classmethod
    def _valid_inspection_test_origins(cls, value: str) -> str:
        parse_test_origins(value)
        return value

    @property
    def public_policy(self) -> PublicUrlPolicy:
        return PublicUrlPolicy(
            allowed_hosts=parse_allowed_hosts(self.public_inspection_hosts),
            test_origins=parse_test_origins(self.inspection_test_origins),
        )

    @field_validator("runtime_token")
    @classmethod
    def _non_trivial_runtime_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("LUMI_RUNTIME_TOKEN must be at least 32 characters")
        return value

    @field_validator("browser_worker_url")
    @classmethod
    def _loopback_worker_only(cls, value: str | None) -> str | None:
        """The worker is a local process. There is no remote browser API."""
        if value is None:
            return None
        url = value.rstrip("/")
        if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("BROWSER_WORKER_URL must address a loopback address")
        return url

    @field_validator("browser_site_origin")
    @classmethod
    def _loopback_site_only(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", value):
            raise ValueError("LUMI_BROWSER_SITE_ORIGIN must be http://127.0.0.1:<port>")
        if not 1 <= int(value.rsplit(":", 1)[1]) <= 65_535:
            raise ValueError("LUMI_BROWSER_SITE_ORIGIN has an invalid port")
        return value

