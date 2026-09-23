import asyncio
import faulthandler
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

from app.config import get_settings
from app.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Our models' MetaData, for autogenerate support.
target_metadata = Base.metadata

# The database URL comes from app.config (env var DATABASE_URL), never
# from alembic.ini, so there is one source of truth and no secret in a
# committed file.
DATABASE_URL = get_settings().DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        # The running deploy keeps serving while the next one migrates, so
        # an ALTER on a busy table can wait on its lock indefinitely -- the
        # first 6e deploy sat silently until Railway's healthcheck gave up.
        # Fail fast and loudly instead; the old deploy keeps running.
        connection.exec_driver_sql("SET LOCAL lock_timeout = '30s'")
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    # Diagnostics for the 6e deploy, where `alembic upgrade head` never
    # returned in the new Docker image: if we are still here after 45 s,
    # dump every thread's stack to stderr (repeating) so the deploy log
    # shows where. Cancelled on the normal path.
    faulthandler.dump_traceback_later(45, repeat=True, file=sys.stderr)
    print("alembic env: running migrations", file=sys.stderr, flush=True)
    asyncio.run(run_async_migrations())
    print("alembic env: migrations done, engine disposed", file=sys.stderr, flush=True)
    faulthandler.cancel_dump_traceback_later()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
