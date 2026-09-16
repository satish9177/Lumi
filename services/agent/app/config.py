from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

AGENT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # Anchored to services/agent so running from the repository root never reads
    # the Electron .env, which holds provider secrets this service must not see.
    model_config = SettingsConfigDict(
        env_file=AGENT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

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
