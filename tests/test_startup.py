"""app/startup.py tests (plan section 5 last line / 16 / 18).

- user_state upsert is idempotent across two boots and leaves exactly
  one row
- an unchanged persona.md inserts no second persona_version
- changed content inserts a new one
"""

from __future__ import annotations


from sqlalchemy import func, select

from app.config import Settings
from app.db.models import PersonaVersion, UserState
from app.startup import run_startup_tasks, sync_persona_version, upsert_user_state

CHAT_ID = 9001


def _settings() -> Settings:
    return Settings(ALLOWED_CHAT_ID=CHAT_ID, TZ_DEFAULT="Europe/Paris")


async def test_user_state_upsert_idempotent_across_two_boots(sessionmaker):
    async with sessionmaker() as session:
        await upsert_user_state(session, CHAT_ID, "Europe/Paris")
    async with sessionmaker() as session:
        await upsert_user_state(session, CHAT_ID, "Europe/Paris")

    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(UserState))
        assert result.scalar_one() == 1

        row = await session.get(UserState, 1)
        assert row.chat_id == CHAT_ID
        assert row.timezone == "Europe/Paris"


async def test_user_state_upsert_refreshes_chat_id_without_resetting_runtime_state(sessionmaker):
    async with sessionmaker() as session:
        await upsert_user_state(session, CHAT_ID, "Europe/Paris")

    # Simulate runtime state changed since the last boot (e.g. by /out).
    async with sessionmaker() as session:
        row = await session.get(UserState, 1)
        row.persona_active = False
        row.intensity = 1
        await session.commit()

    # A new boot with a different configured chat id.
    async with sessionmaker() as session:
        await upsert_user_state(session, 12345, "Europe/Paris")

    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(UserState))
        assert result.scalar_one() == 1

        row = await session.get(UserState, 1)
        assert row.chat_id == 12345
        assert row.persona_active is False
        assert row.intensity == 1


async def test_persona_version_unchanged_content_inserts_no_second_row(sessionmaker, tmp_path):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n\nТы — Anchor.\n", encoding="utf-8")

    async with sessionmaker() as session:
        first = await sync_persona_version(session, persona_path)
    async with sessionmaker() as session:
        second = await sync_persona_version(session, persona_path)

    assert first is True
    assert second is False

    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(PersonaVersion))
        assert result.scalar_one() == 1


async def test_persona_version_changed_content_inserts_new_row(sessionmaker, tmp_path):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n\nТы — Anchor.\n", encoding="utf-8")

    async with sessionmaker() as session:
        await sync_persona_version(session, persona_path)

    persona_path.write_text("# Anchor\n\nТы — Anchor. Изменено.\n", encoding="utf-8")

    async with sessionmaker() as session:
        inserted = await sync_persona_version(session, persona_path)
    assert inserted is True

    async with sessionmaker() as session:
        result = await session.execute(select(func.count()).select_from(PersonaVersion))
        assert result.scalar_one() == 2


async def test_run_startup_tasks_runs_both_steps(sessionmaker, tmp_path):
    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    async with sessionmaker() as session:
        await run_startup_tasks(session, _settings(), persona_path)
    async with sessionmaker() as session:
        await run_startup_tasks(session, _settings(), persona_path)

    async with sessionmaker() as session:
        state_count = await session.execute(select(func.count()).select_from(UserState))
        assert state_count.scalar_one() == 1

        persona_count = await session.execute(select(func.count()).select_from(PersonaVersion))
        assert persona_count.scalar_one() == 1


async def test_startup_with_claude_access_disabled_clears_library_write(sessionmaker, tmp_path):
    """W2b: turning CLAUDE_ACCESS_ENABLED off at startup revokes every
    connection via oauth_store.revoke_everything, which must also clear
    library_write (the same _revoke_connections coupling /revoke,
    /claude disconnect and a replaced connection all share)."""
    import datetime

    from app.db.models import OauthConnection

    persona_path = tmp_path / "persona.md"
    persona_path.write_text("# Anchor\n", encoding="utf-8")

    now = datetime.datetime.now(datetime.timezone.utc)
    async with sessionmaker() as session:
        session.add(
            OauthConnection(
                client_id="c", created_at=now, expires_at=now + datetime.timedelta(days=30),
                library_read=True, library_write=True,
            )
        )
        await session.commit()

    settings = Settings(ALLOWED_CHAT_ID=CHAT_ID, TZ_DEFAULT="Europe/Paris", CLAUDE_ACCESS_ENABLED=False)
    async with sessionmaker() as session:
        await run_startup_tasks(session, settings, persona_path)

    async with sessionmaker() as session:
        row = (await session.execute(select(OauthConnection))).scalars().one()
    assert row.revoked_at is not None
    assert row.library_read is False
    assert row.library_write is False
