"""Milestone 10 S3: registered project recipes -- the rules, with no I/O.

A recipe is created only in trusted UI and binds everything that decides what runs:

* the project root (by identity), the fixed executable (`node.exe` under Program Files, by identity and
  SHA-256) and the fixed npm entry point (`npm-cli.js`, by SHA-256);
* the argument vector `[node.exe, npm-cli.js, "run", <script>]` -- the script NAME is the only variable,
  and it must be a script the project's `package.json` declares;
* `package.json` and lockfile SHA-256 (which covers the script text and its `pre`/`post` scripts);
* an environment allowlist of fixed name/value pairs (secret-looking names refused);
* readiness (an HTTP port and path, or the exit code), a timeout, and the stop policy.

The digest over all of it is the recipe's identity. Anything that changes it invalidates the recipe
(`recipe_changed`), and a model can only ever name a `recipe_id`.
"""

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.digest import canonical_json

PROJECT_RUN_KIND: Final = "project_run"
PROJECT_RUN_POLICY_VERSION: Final = "project-run-v1"
PROJECT_TASK_TYPE: Final = "project_run_task"
TOOL_PROJECT_START: Final = "project_start"
RUN_WARNING: Final = "This recipe executes code from this project with your user-level permissions."

SCRIPT_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,63}$")
ENV_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
READY_PATH: Final = re.compile(r"^/[A-Za-z0-9._~/-]{0,127}$")
MAX_ENV_ENTRIES: Final = 16
MAX_ENV_VALUE_CHARS: Final = 256
MAX_SCRIPT_TEXT_CHARS: Final = 2000
MIN_TIMEOUT_SECONDS: Final = 5
MAX_TIMEOUT_SECONDS: Final = 600

#: Lockfiles, in the order npm itself prefers them.
LOCKFILES: Final = ("npm-shrinkwrap.json", "package-lock.json")

#: Names Lumi sets itself; a recipe may never override them.
RESERVED_ENV: Final = frozenset(
    name.upper()
    for name in (
        "PATH", "PATHEXT", "SystemRoot", "windir", "ComSpec", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "SystemDrive",
    )
)
#: Names that look like credentials or that would change what code runs.
_SECRET_NAME: Final = re.compile(
    r"(KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|CERT|SIGNATURE|BEARER)", re.IGNORECASE
)
_FORBIDDEN_PREFIXES: Final = (
    "LUMI_", "DATABASE", "PG", "AWS_", "AZURE_", "GCP_", "GOOGLE_", "GCLOUD_", "GITHUB_", "GH_", "GIT_", "GITLAB_",
    "NPM_CONFIG_", "YARN_", "PNPM_", "OPENAI", "ANTHROPIC", "GEMINI", "DEEPSEEK", "ELECTRON_", "HTTP_PROXY",
    "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)
#: Names that would make node load or run different code than the recipe names.
_CODE_CHANGING: Final = frozenset({"NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS", "NODE_REPL_EXTERNAL_MODULE"})
_SECRET_VALUE: Final = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[abprs]-|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_-]{20,})")
# eslint-style: C0 controls and DEL.
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
# Bidi overrides and zero-width characters: text that would display differently from what runs.
_INVISIBLE: Final = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


class ProjectRefusal(ValueError):
    """`code` is stable and never carries a path, a value or script text."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The project request was refused ({code}).")
        self.code = code


#: Refusals that mean "the facts moved since you approved": answered 409, not 422.
STATE_CODES: Final = frozenset(
    {
        "project_not_found", "project_revoked", "project_changed", "recipe_not_found", "recipe_changed",
        "recipe_revoked", "grant_not_found", "grant_not_pending", "grant_not_active", "grant_expired",
        "grant_changed", "run_not_found", "run_already_active", "wrong_phase", "effect_locked",
        "executable_changed",
    }
)


def validate_script_name(value: str) -> str:
    if not isinstance(value, str) or not SCRIPT_NAME.fullmatch(value):
        raise ProjectRefusal("script_name_invalid")
    return value


def validate_env(entries: dict[str, str]) -> dict[str, str]:
    """Fixed name/value pairs the person typed. Refuses secret-looking names or values and anything that
    would change which code runs or where it looks for credentials."""
    if not isinstance(entries, dict) or len(entries) > MAX_ENV_ENTRIES:
        raise ProjectRefusal("env_invalid")
    clean: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in entries.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ProjectRefusal("env_name_invalid")
        upper = name.upper()
        if upper in seen:
            raise ProjectRefusal("env_name_duplicate")
        seen.add(upper)
        if upper in RESERVED_ENV or upper in _CODE_CHANGING or upper.startswith(_FORBIDDEN_PREFIXES):
            raise ProjectRefusal("env_name_reserved")
        if _SECRET_NAME.search(name):
            raise ProjectRefusal("env_name_secret")
        if not isinstance(value, str) or len(value) > MAX_ENV_VALUE_CHARS or _CONTROL.search(value):
            raise ProjectRefusal("env_value_invalid")
        if _SECRET_VALUE.search(value):
            raise ProjectRefusal("env_value_secret")
        clean[name] = value
    return clean


@dataclass(frozen=True, slots=True)
class Readiness:
    kind: Literal["http", "exit_code"]
    port: int | None = None
    path: str | None = None

    @staticmethod
    def of(kind: str, port: int | None, path: str | None) -> "Readiness":
        if kind == "exit_code":
            if port is not None or path is not None:
                raise ProjectRefusal("readiness_invalid")
            return Readiness("exit_code")
        if kind != "http":
            raise ProjectRefusal("readiness_invalid")
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
            raise ProjectRefusal("readiness_port_invalid")
        chosen = path or "/"
        if not READY_PATH.fullmatch(chosen) or ".." in chosen:
            raise ProjectRefusal("readiness_path_invalid")
        return Readiness("http", port, chosen)


def validate_timeout(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise ProjectRefusal("timeout_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ExecutableFacts:
    """A pinned file: where, which file (volume, index, size) and exactly which bytes."""

    path: str
    volume: int
    index: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PackageFacts:
    """What `package.json` and the lockfile say, and their hashes."""

    package_sha256: str
    lockfile_name: str | None
    lockfile_sha256: str | None
    #: The project's own `.npmrc`, which npm reads from the working directory: it can change how a script
    #: runs, so it is part of what a recipe freezes (None when the project has none).
    npmrc_sha256: str | None = None
    scripts: dict[str, str] = field(default_factory=dict)
    has_dependencies: bool = False
    #: EVERY script name npm would run (truthy value), displayable or not: a hook that cannot be shown
    #: must refuse the recipe rather than run unseen (S3 review finding 2).
    runnable_names: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RecipeSpec:
    project_id: str
    project_volume: int
    project_index: int
    label: str
    script: str
    script_text: str
    pre_text: str | None
    post_text: str | None
    package_sha256: str
    lockfile_name: str | None
    lockfile_sha256: str | None
    npmrc_sha256: str | None
    node: ExecutableFacts
    npm_cli: ExecutableFacts
    env: dict[str, str]
    readiness: Readiness
    timeout_seconds: int
    stop_policy: Literal["terminate_job"] = "terminate_job"

    def argv_shape(self) -> list[str]:
        return ["node.exe", "npm-cli.js", "run", self.script]

    def digest(self) -> str:
        payload = asdict(self)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_package(data: bytes) -> tuple[dict[str, str], bool, frozenset[str]]:
    """(displayable scripts, declares dependencies, every runnable script name) from `package.json` bytes."""
    if len(data) > 1024 * 1024:
        raise ProjectRefusal("package_json_too_large")
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProjectRefusal("package_json_invalid") from None
    if not isinstance(parsed, dict):
        raise ProjectRefusal("package_json_invalid")
    raw_scripts = parsed.get("scripts") or {}
    if not isinstance(raw_scripts, dict):
        raise ProjectRefusal("package_json_invalid")
    scripts = {
        name: text
        for name, text in raw_scripts.items()
        if isinstance(name, str) and isinstance(text, str) and SCRIPT_NAME.fullmatch(name) and len(text) <= MAX_SCRIPT_TEXT_CHARS
        and not _CONTROL.search(text) and not _INVISIBLE.search(text)
    }
    runnable = frozenset(str(name) for name, text in raw_scripts.items() if text)
    has_dependencies = any(isinstance(parsed.get(key), dict) and parsed[key] for key in ("dependencies", "devDependencies"))
    return scripts, has_dependencies, runnable


#: Commands a script may start with without a `node_modules/.bin` entry.
_BUILTIN_COMMANDS: Final = frozenset({"node", "npm"})


def first_command(script_text: str) -> str | None:
    """The first command a script runs, for the missing-dependency pre-check (never for execution)."""
    tokens = [token for token in re.split(r"\s+", script_text.strip()) if token]
    for token in tokens:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue  # VAR=value prefix
        return token.strip("\"'")
    return None


def needs_bin(command: str | None) -> bool:
    return command is not None and command not in _BUILTIN_COMMANDS and re.fullmatch(r"[A-Za-z0-9@._-]{1,64}", command) is not None


def run_effect_key_value(project_id: str) -> str:
    return f"project:{project_id}"


def safe_log_line(text: str) -> str:
    return _CONTROL.sub("", text)[:400]


class ProjectRunScope(BaseModel):
    """Exactly what one `project_run` grant authorises: ONE start of ONE recipe revision. Immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["project_run"] = PROJECT_RUN_KIND
    policy_version: Literal["project-run-v1"] = PROJECT_RUN_POLICY_VERSION
    task_id: uuid.UUID
    run_id: uuid.UUID
    recipe_id: uuid.UUID
    recipe_revision: int = Field(ge=1)
    recipe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: uuid.UUID
    project_label: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    script: str = Field(pattern=SCRIPT_NAME.pattern)
    script_text: str = Field(max_length=MAX_SCRIPT_TEXT_CHARS)
    pre_text: str | None = Field(default=None, max_length=MAX_SCRIPT_TEXT_CHARS)
    post_text: str | None = Field(default=None, max_length=MAX_SCRIPT_TEXT_CHARS)
    argv: tuple[str, ...]
    env_names: tuple[str, ...]
    readiness_kind: Literal["http", "exit_code"]
    ready_port: int | None = None
    ready_path: str | None = None
    timeout_seconds: int = Field(ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)
    stop_policy: Literal["terminate_job"] = "terminate_job"
    warning: Literal["This recipe executes code from this project with your user-level permissions."] = RUN_WARNING
    #: One start. Never a second from the same approval.
    steps: tuple[Literal["project_start"], ...] = (TOOL_PROJECT_START,)

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode("utf-8")).hexdigest()


def run_scope(
    spec: RecipeSpec, *, task_id: uuid.UUID, run_id: uuid.UUID, recipe_id: uuid.UUID, recipe_revision: int,
    recipe_digest: str, project_label: str,
) -> ProjectRunScope:
    return ProjectRunScope(
        task_id=task_id,
        run_id=run_id,
        recipe_id=recipe_id,
        recipe_revision=recipe_revision,
        recipe_digest=recipe_digest,
        project_id=uuid.UUID(spec.project_id),
        project_label=project_label,
        label=spec.label,
        script=spec.script,
        script_text=spec.script_text,
        pre_text=spec.pre_text,
        post_text=spec.post_text,
        argv=tuple(spec.argv_shape()),
        env_names=tuple(sorted(spec.env)),
        readiness_kind=spec.readiness.kind,
        ready_port=spec.readiness.port,
        ready_path=spec.readiness.path,
        timeout_seconds=spec.timeout_seconds,
    )


def spec_from_json(raw: dict[str, object]) -> RecipeSpec:
    """The stored spec back into its frozen form (the digest is recomputed and compared by the caller)."""
    node = raw["node"]
    npm = raw["npm_cli"]
    readiness = raw["readiness"]
    assert isinstance(node, dict) and isinstance(npm, dict) and isinstance(readiness, dict)
    env = raw["env"]
    assert isinstance(env, dict)
    return RecipeSpec(
        project_id=str(raw["project_id"]),
        project_volume=int(str(raw["project_volume"])),
        project_index=int(str(raw["project_index"])),
        label=str(raw["label"]),
        script=str(raw["script"]),
        script_text=str(raw["script_text"]),
        pre_text=None if raw.get("pre_text") is None else str(raw["pre_text"]),
        post_text=None if raw.get("post_text") is None else str(raw["post_text"]),
        package_sha256=str(raw["package_sha256"]),
        lockfile_name=None if raw.get("lockfile_name") is None else str(raw["lockfile_name"]),
        lockfile_sha256=None if raw.get("lockfile_sha256") is None else str(raw["lockfile_sha256"]),
        npmrc_sha256=None if raw.get("npmrc_sha256") is None else str(raw["npmrc_sha256"]),
        node=ExecutableFacts(**{key: node[key] for key in ("path", "volume", "index", "size", "sha256")}),
        npm_cli=ExecutableFacts(**{key: npm[key] for key in ("path", "volume", "index", "size", "sha256")}),
        env={str(key): str(value) for key, value in env.items()},
        readiness=Readiness(kind=readiness["kind"], port=readiness.get("port"), path=readiness.get("path")),
        timeout_seconds=int(str(raw["timeout_seconds"])),
    )
