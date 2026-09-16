"""Fixed schema-upgrade entry point for the trusted desktop supervisor.

The runtime itself never creates or alters tables; it refuses to serve against
a database that is not at the Alembic head. An installed desktop app has no
developer shell to run `alembic upgrade head`, so Electron main runs this
module first, with the same constructed environment it gives the runtime
(`DATABASE_URL` only; no runtime credential is needed to migrate).

It prints nothing but a fixed status word: the database URL, which carries a
password, must never reach a log.
"""

import sys

from alembic import command

from app.config import DatabaseSettings
from app.db.migrations import alembic_config


def main() -> int:
    try:
        settings = DatabaseSettings()
    except Exception:  # noqa: BLE001 - the message could echo configuration.
        print("lumi-migrate: configuration-invalid", file=sys.stderr)
        return 2
    try:
        command.upgrade(
            alembic_config(settings.database_url.get_secret_value(), configure_logging=False),
            "head",
        )
    except Exception:  # noqa: BLE001 - driver errors can contain the URL.
        print("lumi-migrate: failed", file=sys.stderr)
        return 1
    print("lumi-migrate: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
