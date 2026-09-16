from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import AGENT_ROOT


class SchemaNotCurrentError(RuntimeError):
    pass


def alembic_config(database_url: str | None = None, *, configure_logging: bool = True) -> Config:
    config = Config(str(AGENT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(AGENT_ROOT / "alembic"))
    # Passed as an attribute, not a main option, so '%' in a password is never
    # interpolated by ConfigParser.
    if database_url is not None:
        config.attributes["database_url"] = database_url
    config.attributes["configure_logging"] = configure_logging
    return config


def _current_heads(connection: Connection) -> tuple[str, ...]:
    return MigrationContext.configure(connection).get_current_heads()


async def verify_schema_is_current(engine: AsyncEngine) -> None:
    """Refuse to serve against an unmigrated database instead of creating tables."""
    expected = set(ScriptDirectory.from_config(alembic_config()).get_heads())
    async with engine.connect() as connection:
        current = set(await connection.run_sync(_current_heads))
    if current != expected:
        raise SchemaNotCurrentError(
            f"Database schema is at {sorted(current) or 'no revision'}, expected "
            f"{sorted(expected)}. Run `uv run alembic upgrade head` in services/agent."
        )
