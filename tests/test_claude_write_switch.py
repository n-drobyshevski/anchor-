"""The write switch, `/claude library write on|off` (W2b, plan section 5).
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from app.db.models import OauthConnection
from app.tg import claude as claude_ui
from app.web import oauth_store
from claude_helpers import World, settings

pytestmark = pytest.mark.asyncio


async def _world(sessionmaker, **overrides) -> World:
    world = World(sessionmaker, settings(**overrides))
    await world.seed()
    return world


async def _connection(sessionmaker) -> OauthConnection:
    async with sessionmaker() as session:
        return (await session.execute(select(OauthConnection))).scalars().one()


async def test_write_on_needs_read_on_first(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    assert await world.command("/claude library write on") == claude_ui.LIBRARY_WRITE_NEEDS_READ
    connection = await _connection(sessionmaker)
    assert connection.library_write is False


async def test_write_on_after_read_on(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    assert await world.command("/claude library write on") == claude_ui.LIBRARY_WRITE_SET_ON
    connection = await _connection(sessionmaker)
    assert connection.library_write is True
    assert "Библиотека: включена · запись включена." in await world.command("/claude")


async def test_write_off(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    assert await world.command("/claude library write off") == claude_ui.LIBRARY_WRITE_SET_OFF
    connection = await _connection(sessionmaker)
    assert connection.library_write is False
    assert "Библиотека: включена · запись выключена." in await world.command("/claude")


async def test_read_off_clears_write(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    await world.command("/claude library off")
    connection = await _connection(sessionmaker)
    assert connection.library_read is False
    assert connection.library_write is False
    assert "Библиотека: выключена." in await world.command("/claude")


async def test_write_no_connection(sessionmaker):
    world = await _world(sessionmaker)
    assert await world.command("/claude library write on") == claude_ui.LIBRARY_NO_CONNECTION


async def test_write_usage_on_a_bad_word(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    assert await world.command("/claude library write maybe") == claude_ui.LIBRARY_WRITE_USAGE


async def test_revoke_clears_both_switches(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    await world.command("/revoke")
    connection = await _connection(sessionmaker)
    assert connection.library_read is False
    assert connection.library_write is False


async def test_disconnect_removes_the_connection_entirely(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    await world.command("/claude disconnect")
    async with sessionmaker() as session:
        current = await oauth_store.current_connection(session, world.clock)
        row = (await session.execute(select(OauthConnection))).scalars().one()
    # disconnect revokes -- it does not drop the row (rows are Claude's
    # only history of the connection until /delete); "no connection" is
    # what current_connection (the only thing app/tg/claude.py asks) sees.
    assert current is None
    assert row.revoked_at is not None
    assert row.library_read is False
    assert row.library_write is False


async def test_a_replaced_connection_starts_both_switches_off(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
        await world.command("/claude library on")
        await world.command("/claude library write on")
        await world.connect(client)  # a second connection, replacing the first
    async with sessionmaker() as session:
        connection = await oauth_store.current_connection(session, world.clock)
    assert connection.library_read is False
    assert connection.library_write is False


async def test_delete_wipes_the_connection_and_its_switches(sessionmaker):
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    await world.command("/delete")
    kb = world.tg_fake.sent[-1].reply_markup
    await world.press(kb.inline_keyboard[0][0].callback_data)
    async with sessionmaker() as session:
        rows = (await session.execute(select(OauthConnection))).scalars().all()
    assert rows == []


# --- proof: a breaking edit to set_library's coupling is caught --------


async def test_set_library_off_without_the_write_coupling_would_be_caught(sessionmaker, monkeypatch):
    """Prove test_read_off_clears_write actually exercises the coupling
    in app/web/oauth_store.py's set_library, by breaking it and
    watching the test above fail."""
    from app.core.clock import Clock
    from sqlalchemy.ext.asyncio import AsyncSession

    async def _broken_set_library(session: AsyncSession, clock: Clock, on: bool):
        connection = await oauth_store.current_connection(session, clock)
        if connection is None:
            return None
        connection.library_read = on
        # deliberately drop the "on is False also clears write" line
        await session.commit()
        return connection

    monkeypatch.setattr(oauth_store, "set_library", _broken_set_library)
    world = await _world(sessionmaker)
    async with TestClient(TestServer(world.app)) as client:
        await world.connect(client)
    await world.command("/claude library on")
    await world.command("/claude library write on")
    await world.command("/claude library off")
    connection = await _connection(sessionmaker)
    # With the coupling removed, write incorrectly survives read going off.
    assert connection.library_write is True
