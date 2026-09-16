import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

SECRET = "correct-horse-battery-staple"


def test_accepts_asyncpg_url() -> None:
    settings = Settings(database_url=SecretStr(f"postgresql+asyncpg://lumi:{SECRET}@127.0.0.1/lumi_agent"))
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
        Settings(database_url=SecretStr(url))
    assert SECRET not in str(caught.value)


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)
