import re
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.browser.profile_paths import (
    PROFILE_ROOT_VARIABLE,
    ProfilePathError,
    ProfilePaths,
    resolve_profile_paths,
)
from app.domain.browser_profile import DEFAULT_LEASE_TTL_SECONDS
from app.domain.public_url import (
    RESEARCH_POLICY_VERSION,
    PublicUrlPolicy,
    parse_allowed_hosts,
    parse_test_origins,
)

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
    #: Milestone 9 S1: Windows semantic observation. Off unless explicitly enabled, exactly
    #: like the browser worker: a deployment that has no business reading other applications
    #: cannot acquire the capability by accident. Even when enabled, the isolated desktop
    #: worker starts only on the first request, and nothing in the product calls it yet.
    desktop_observation: bool = Field(default=False, validation_alias="LUMI_DESKTOP_OBSERVATION")
    desktop_observation_timeout_seconds: float = Field(
        default=30.0, gt=0, le=120, validation_alias="LUMI_DESKTOP_TIMEOUT_SECONDS"
    )
    #: Milestone 9 S3: the user's registered applications, a JSON list of
    #: `{"appId", "label", "executable", "args"?}`. Trusted configuration set by Electron main from the user's own
    #: settings; no request, renderer field or model can add to it. Validated here so a bad document stops the
    #: runtime at startup instead of being half applied.
    desktop_registered_apps: str = Field(default="", max_length=8_192, validation_alias="LUMI_DESKTOP_REGISTERED_APPS")
    #: Milestone 9 S2: how long one desktop-disclosure approval stays usable. It is single-use either
    #: way; expiry never makes it reusable.
    desktop_disclosure_ttl_seconds: int = Field(
        default=600, ge=30, le=1_800, validation_alias="LUMI_DESKTOP_DISCLOSURE_TTL_SECONDS"
    )
    #: Milestone 10 S1: how long one document-disclosure approval stays usable. Single-use either way.
    document_disclosure_ttl_seconds: int = Field(
        default=600, ge=30, le=1_800, validation_alias="LUMI_DOCUMENT_DISCLOSURE_TTL_SECONDS"
    )
    #: Milestone 10 S2: the download quarantine's base directory (Lumi-owned). Empty means
    #: `%LOCALAPPDATA%\Lumi\quarantine`. The browser worker receives the same value.
    download_quarantine_root: str = Field(default="", max_length=1024, validation_alias="LUMI_DOWNLOAD_QUARANTINE_ROOT")
    transfer_grant_ttl_seconds: int = Field(default=600, ge=30, le=1_800, validation_alias="LUMI_TRANSFER_TTL_SECONDS")
    #: Milestone 10 S3: per-run scratch folders (empty npm config, TEMP, HOME). Default %LOCALAPPDATA%\Lumi\project-runs.
    project_run_root: str = Field(default="", max_length=1024, validation_alias="LUMI_PROJECT_RUN_ROOT")
    project_run_ttl_seconds: int = Field(default=600, ge=30, le=1_800, validation_alias="LUMI_PROJECT_RUN_TTL_SECONDS")
    #: Milestone 7a public page inspection. Hosts (`github.com,*.example.org`)
    #: an approved inspection may open, and exact loopback test origins. Both
    #: empty (the default) means there is no public inspection capability.
    #: Supplied by Electron main from its own trusted configuration.
    public_inspection_hosts: str = Field(default="", validation_alias="LUMI_PUBLIC_INSPECTION_HOSTS")
    inspection_test_origins: str = Field(default="", validation_alias="LUMI_INSPECTION_TEST_ORIGINS")

    @field_validator("desktop_registered_apps")
    @classmethod
    def _valid_registered_apps(cls, value: str) -> str:
        from app.desktop.registry import AppRegistry

        try:
            AppRegistry.from_config(value)
        except (ValueError, TypeError):
            raise ValueError("LUMI_DESKTOP_REGISTERED_APPS is not a valid registered-application list") from None
        return value

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

    # --- Milestone 7b public research ------------------------------------
    #
    # Research is a separate capability from Milestone 7a inspection, with its
    # own destination policy. Inspection keeps its host allowlist whatever
    # research allows; nothing here widens it.
    #
    # `research_any_public_host` is the honest name for what research needs: a
    # research task cannot know its destinations in advance, so the allowlist
    # layer is replaced by "not local, not private, not reserved, and resolving
    # only to globally routable addresses". The residual DNS-rebinding gap that
    # leaves is documented in `app/browser/network_guard.py`.
    research_any_public_host: bool = Field(
        default=False, validation_alias="LUMI_RESEARCH_ANY_PUBLIC_HOST"
    )
    research_hosts: str = Field(default="", validation_alias="LUMI_RESEARCH_HOSTS")
    research_test_origins: str = Field(default="", validation_alias="LUMI_RESEARCH_TEST_ORIGINS")
    #: Milestone 8a S2. Exact loopback origins of the synthetic login/SSO
    #: fixture the takeover test suite drives. Empty in every build that does
    #: not test manual login.
    auth_test_origins: str = Field(default="", validation_alias="LUMI_AUTH_TEST_ORIGINS")

    @field_validator("auth_test_origins")
    @classmethod
    def _valid_auth_test_origins(cls, value: str) -> str:
        parse_test_origins(value)
        return value
    research_max_tabs: int = Field(
        default=5, ge=1, le=5, validation_alias="LUMI_RESEARCH_MAX_TABS"
    )
    #: Milestone 8a S3 authenticated reading. Smaller than research on purpose:
    #: a confirmed scope lasts ten minutes at most, a step authorization is
    #: minted and consumed inside one request, and an account read never has
    #: more than three tabs.
    authenticated_grant_ttl_seconds: int = Field(
        default=600, ge=30, le=1_800, validation_alias="LUMI_AUTHENTICATED_GRANT_TTL_SECONDS"
    )
    authenticated_step_ttl_seconds: int = Field(
        default=120, ge=5, le=600, validation_alias="LUMI_AUTHENTICATED_STEP_TTL_SECONDS"
    )
    authenticated_max_tabs: int = Field(
        default=3, ge=1, le=3, validation_alias="LUMI_AUTHENTICATED_MAX_TABS"
    )
    #: How long one confirmed research scope authorises steps for. Ten minutes
    #: initially; configurable because it is a tuning value, not an invariant.
    research_grant_ttl_seconds: int = Field(
        default=600, ge=30, le=3_600, validation_alias="LUMI_RESEARCH_GRANT_TTL_SECONDS"
    )
    #: How long a single-use step authorization may be claimed for. Short: it
    #: is minted and consumed inside one request.
    research_step_ttl_seconds: int = Field(
        default=120, ge=5, le=600, validation_alias="LUMI_RESEARCH_STEP_TTL_SECONDS"
    )
    #: The public search endpoint, a URL template containing `{query}`. It is a
    #: bounded JSON GET the *runtime* makes, so no search credential and no
    #: search-result markup ever reaches the browser worker. Empty means
    #: research has no search primitive; navigation and links still work.
    research_search_endpoint: str = Field(
        default="", validation_alias="LUMI_RESEARCH_SEARCH_ENDPOINT"
    )
    research_search_api_key: SecretStr | None = Field(
        default=None, validation_alias="LUMI_RESEARCH_SEARCH_API_KEY"
    )
    research_search_header: str = Field(
        default="X-Subscription-Token", validation_alias="LUMI_RESEARCH_SEARCH_HEADER"
    )
    research_search_timeout_seconds: float = Field(
        default=15.0, gt=0, le=60, validation_alias="LUMI_RESEARCH_SEARCH_TIMEOUT_SECONDS"
    )

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

    @field_validator("research_search_header")
    @classmethod
    def _valid_search_header(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", value):
            raise ValueError("LUMI_RESEARCH_SEARCH_HEADER must be a plain header name")
        return value

    # --- Milestone 8a S1 persistent browser profiles ----------------------
    #
    # A *base* directory, never a profile path: the runtime and the worker each
    # append the profile UUID themselves, so nothing a caller supplies ever
    # becomes a path component. Empty (the default) means
    # `%LOCALAPPDATA%\Lumirowser-profiles`, which is what a packaged install
    # uses; the test suite points it at a temporary directory.
    browser_profile_root: str = Field(default="", validation_alias="LUMI_BROWSER_PROFILE_ROOT")
    #: How long one runtime generation holds a profile lease before another may
    #: reclaim it -- and then only when the OS-level handle is also free.
    browser_profile_lease_ttl_seconds: int = Field(
        default=DEFAULT_LEASE_TTL_SECONDS,
        ge=30,
        le=3_600,
        validation_alias="LUMI_BROWSER_PROFILE_LEASE_TTL_SECONDS",
    )
    #: Recorded on a profile row alongside the Chromium and Playwright versions
    #: that last opened it, so a support question can be answered from metadata.
    app_version: str = Field(default="0.1.0", max_length=32, validation_alias="LUMI_APP_VERSION")

    # --- Milestone 8a S2 manual login and human takeover -------------------
    #
    # The hard timeout on a takeover: how long the human may control the
    # browser before Lumi closes the window and returns the profile to
    # NEEDS_LOGIN without any assumption about whether the website session
    # persisted. Fifteen minutes by default; configurable within a range that
    # keeps it a bounded, visible interval rather than an unattended one.
    login_attempt_ttl_seconds: int = Field(
        default=900, ge=60, le=3_600, validation_alias="LUMI_LOGIN_ATTEMPT_TTL_SECONDS"
    )
    #: How often the runtime sweeps for expired takeovers and closes their
    #: headed windows. Independent of the timeout itself.
    login_attempt_sweep_interval_seconds: float = Field(
        default=15.0, gt=0, le=300, validation_alias="LUMI_LOGIN_ATTEMPT_SWEEP_INTERVAL_SECONDS"
    )

    @property
    def profile_paths(self) -> ProfilePaths | None:
        """Where persistent profiles live for this runtime process.

        Resolved from this process's own environment, exactly as the worker
        resolves its own copy, and never from a request or a model. `None`
        means "resolve it when a profile operation actually needs it", so a
        machine without `%LOCALAPPDATA%` fails that operation rather than
        refusing to start the runtime at all.
        """
        environment = (
            {PROFILE_ROOT_VARIABLE: self.browser_profile_root}
            if self.browser_profile_root
            else None
        )
        try:
            return resolve_profile_paths(environment)
        except ProfilePathError:
            return None

    @property
    def research_policy(self) -> PublicUrlPolicy:
        return PublicUrlPolicy(
            allowed_hosts=parse_allowed_hosts(self.research_hosts),
            test_origins=parse_test_origins(self.research_test_origins),
            version=RESEARCH_POLICY_VERSION,
            allow_any_public_host=self.research_any_public_host,
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

