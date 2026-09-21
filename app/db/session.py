"""Async SQLAlchemy engine/session factory.

Deliberately not a module-level singleton: create_engine_and_sessionmaker()
is a factory so main.py builds one for the running app and tests build
their own, pointed at a throwaway database.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    return engine, sessionmaker


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
