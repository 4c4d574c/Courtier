import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Add monorepo source paths so we can import courtier modules and domains.
_project_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "domains", "docaudit"))
sys.path.insert(0, os.path.join(_project_root, "libs", "shared"))
sys.path.insert(0, os.path.join(_project_root, "libs", "docaudit"))

from courtier.db.tables import Base  # noqa: E402

target_metadata = Base.metadata


def get_url() -> str:
    """Read the database URL from the application settings snapshot."""
    from courtier.config import get_settings  # noqa: E402

    return get_settings().mysql_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode with an async engine."""
    connectable = create_async_engine(
        get_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
