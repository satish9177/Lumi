import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

SECRET = "correct-horse-battery-staple"
RUNTIME_TOKEN = SecretStr("test-runtime-token-with-at-least-32-bytes")


def settings_for(url: str, *, approval_ttl_seconds: int = 300) -> Settings:
    return Settings(
        database_url=SecretStr(url),
        runtime_token=RUNTIME_TOKEN,
        approval_ttl_seconds=approval_ttl_seconds,
    )


def test_accepts_asyncpg_url() -> None:
    settings = settings_for(f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/lumi_agent")
    assert SECRET not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://lumi:{SECRET}@127.0.0.1/lumi_agent",
        f"sqlite+aiosqlite:///{SECRET}.db",
        f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/",
        "not a url",
    ],
)
def test_rejects_unusable_urls_without_echoing_them(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        settings_for(url)
    assert SECRET not in str(caught.value)


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, runtime_token=RUNTIME_TOKEN)


def test_approval_ttl_defaults_to_five_minutes() -> None:
    settings = settings_for(f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/lumi_agent")
    assert settings.approval_ttl_seconds == 300


@pytest.mark.parametrize("ttl", [0, -1, 100_000])
def test_rejects_an_unusable_approval_ttl(ttl: int) -> None:
    # A zero or negative TTL would make every approval unusable; a very long one
    # would make "the user agreed to this" mean very little.
    with pytest.raises(ValidationError, match="approval_ttl_seconds"):
        settings_for(
            f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/lumi_agent",
            approval_ttl_seconds=ttl,
        )


def test_runtime_token_is_required_and_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUMI_RUNTIME_TOKEN", raising=False)
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            database_url=SecretStr(
                f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/lumi_agent"
            ),
        )
    assert SECRET not in str(caught.value)


def test_runtime_token_must_be_high_entropy_length() -> None:
    with pytest.raises(ValidationError, match="LUMI_RUNTIME_TOKEN"):
        Settings(
            database_url=SecretStr(
                f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/lumi_agent"
            ),
            runtime_token=SecretStr("too-short"),
        )


def test_migrations_do_not_need_a_runtime_credential(migrated_database_url: str) -> None:
    """`uv run alembic upgrade head` runs outside any runtime generation."""
    import os
    import subprocess
    import sys

    from app.config import AGENT_ROOT

    environment = {key: value for key, value in os.environ.items() if key != "LUMI_RUNTIME_TOKEN"}
    environment["DATABASE_URL"] = migrated_database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=AGENT_ROOT, env=environment, capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, "alembic upgrade head failed without LUMI_RUNTIME_TOKEN"
