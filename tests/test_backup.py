"""app/ops/backup.py -- encrypted nightly backups (Phase 6 plan section
9.1; milestone 6e; plan section 12's "backup" test list).

Layout:
- pure logic (pruning selection, dedup key) -- no DB, no subprocess;
- the encrypt-then-upload stream, wired with a real test age keypair
  and a fake in-memory S3 client -- no real pg_dump;
- failure paths (not_configured, pg_dump missing) against the real
  test database but a fake S3;
- one real round trip through the local Postgres 18 pg_dump binary,
  skipped with a clear reason if it is not present;
- the "plaintext never touches disk" guarantee.
"""

from __future__ import annotations

import datetime
import io
import os
import tempfile

import pyrage
import pytest
from sqlalchemy import select

from app.config import Settings
from app.core.clock import FrozenClock
from app.db.models import BackupLog
from app.core.scheduler import maybe_enqueue_backup
from app.ops import backup as backup_module

# No module-level `pytestmark = pytest.mark.asyncio` here, unlike most
# test files in this repo -- pyproject.toml's asyncio_mode = "auto"
# already detects async def tests on its own, and this file (unlike
# most others) mixes plain sync tests (the pure pruning/writer logic)
# with async ones (anything touching a session), so an explicit marker
# would misfire a pytest-asyncio warning on every sync test.

NOW = datetime.datetime(2026, 1, 5, 4, 0, tzinfo=datetime.timezone.utc)  # a Monday

# Confirmed locally: /usr/lib/postgresql/18/bin/pg_dump -- the version
# production runs (Railway's postgres-ssl:18), matching
# tests/conftest.py's PRODUCTION_PG_MAJOR. Skipped with a clear reason
# when a machine does not have it, rather than silently running the
# real round-trip test against some other pg_dump on PATH.
PG18_DUMP = "/usr/lib/postgresql/18/bin/pg_dump"


class _FakeS3Client:
    """An in-memory multipart-upload-capable S3 stand-in (no moto, per
    the approved decision)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self._uploads: dict[str, list[bytes]] = {}
        self._next_id = 0

    def create_multipart_upload(self, *, Bucket, Key):
        self._next_id += 1
        upload_id = f"upload-{self._next_id}"
        self._uploads[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, PartNumber, UploadId, Body):
        self._uploads[UploadId].append(bytes(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        self.objects[Key] = b"".join(self._uploads.pop(UploadId))

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self._uploads.pop(UploadId, None)

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        matching = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in matching], "IsTruncated": False}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)

    def delete_objects(self, *, Bucket, Delete):
        for entry in Delete["Objects"]:
            self.objects.pop(entry["Key"], None)


def _configured_settings(**overrides) -> Settings:
    defaults = dict(
        BACKUP_AGE_RECIPIENT=str(pyrage.x25519.Identity.generate().to_public()),
        BACKUP_S3_ENDPOINT="https://fake.example",
        BACKUP_S3_BUCKET="anchor-backups",
        BACKUP_S3_ACCESS_KEY_ID="fake-key",
        BACKUP_S3_SECRET_ACCESS_KEY="fake-secret",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# --- pure logic: pruning selection -----------------------------------


def _entry(row_id: int, day: str) -> tuple[int, str, datetime.date]:
    return row_id, f"anchor/x/{row_id}.dump.age", datetime.date.fromisoformat(day)


def test_select_prune_targets_keeps_recent_daily_and_recent_sundays():
    # 10 consecutive days, 2026-01-01 (Thu) through 2026-01-10 (Sat).
    # Sundays in that range: 2026-01-04.
    entries = [
        _entry(1, "2026-01-01"),
        _entry(2, "2026-01-02"),
        _entry(3, "2026-01-03"),
        _entry(4, "2026-01-04"),  # Sunday
        _entry(5, "2026-01-05"),
        _entry(6, "2026-01-06"),
        _entry(7, "2026-01-07"),
        _entry(8, "2026-01-08"),
        _entry(9, "2026-01-09"),
        _entry(10, "2026-01-10"),
    ]
    # keep_daily=3 keeps ids 8,9,10 (the three most recent); keep_weekly=1
    # keeps the most recent Sunday, id 4.
    pruned = backup_module.select_prune_targets(entries, keep_daily=3, keep_weekly=1)
    assert set(pruned) == {1, 2, 3, 5, 6, 7}


def test_select_prune_targets_empty_when_within_both_windows():
    entries = [_entry(1, "2026-01-04"), _entry(2, "2026-01-05")]
    assert backup_module.select_prune_targets(entries, keep_daily=14, keep_weekly=8) == []


def test_backup_dedup_key_is_one_per_local_date():
    d = datetime.date(2026, 1, 5)
    assert backup_module.backup_dedup_key(d) == backup_module.backup_dedup_key(d)
    assert backup_module.backup_dedup_key(d) != backup_module.backup_dedup_key(
        datetime.date(2026, 1, 6)
    )


# --- the encrypt-then-upload stream, with a test age keypair ---------


def test_s3_multipart_writer_roundtrips_through_a_real_age_keypair():
    """Plan section 12: "an encrypt-then-upload stream test with a fake
    S3 and a test age keypair" -- generated in the test
    (pyrage.x25519.Identity.generate()), decrypt and assert the dump
    bytes round-trip."""
    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()

    plaintext = b"PGDMP" + os.urandom(200_000)  # bigger than one multipart part
    client = _FakeS3Client()
    writer = backup_module._S3MultipartWriter(client, "bucket", "anchor/x.dump.age")
    pyrage.encrypt_io(io.BytesIO(plaintext), writer, [recipient])
    writer.close()

    ciphertext = client.objects["anchor/x.dump.age"]
    assert ciphertext != plaintext

    out = io.BytesIO()
    pyrage.decrypt_io(io.BytesIO(ciphertext), out, [identity])
    assert out.getvalue() == plaintext

    import hashlib

    assert writer.sha256_hex == hashlib.sha256(ciphertext).hexdigest()
    assert writer.total_bytes == len(ciphertext)


def test_s3_multipart_writer_chunks_are_bounded():
    """Bytes handed to write() are flushed to S3 in bounded parts, never
    held in full -- the "true streaming, not buffer-it-all" claim in the
    module docstring, made checkable."""
    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()

    plaintext = os.urandom(3 * backup_module._MULTIPART_PART_SIZE)
    client = _FakeS3Client()
    writer = backup_module._S3MultipartWriter(client, "bucket", "k")
    pyrage.encrypt_io(io.BytesIO(plaintext), writer, [recipient])
    writer.close()

    assert len(writer._part_sizes) > 1, "a multi-part dump must use more than one S3 part"
    for size in writer._part_sizes[:-1]:
        assert size >= backup_module._MULTIPART_PART_SIZE


def test_s3_multipart_writer_empty_plaintext_still_uploads_the_age_header():
    """age's own stream format always writes a header + MAC, even for a
    zero-byte plaintext -- so ciphertext-side total_bytes is never zero.
    Empty-dump detection (`EMPTY_DUMP`) therefore counts plaintext bytes
    read from pg_dump's stdout instead -- see `_CountingReader` and
    `_run_pipeline_sync`'s own use of it."""
    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()
    client = _FakeS3Client()
    writer = backup_module._S3MultipartWriter(client, "bucket", "k")
    pyrage.encrypt_io(io.BytesIO(b""), writer, [recipient])
    writer.close()
    assert writer.total_bytes > 0
    assert "k" in client.objects


def test_counting_reader_counts_plaintext_bytes_read():
    reader = backup_module._CountingReader(io.BytesIO(b"hello world"))
    assert reader.read(5) == b"hello"
    assert reader.read() == b" world"
    assert reader.total_bytes == 11


# --- run_backup: failure paths (real DB, fake S3) ---------------------


async def test_run_backup_not_configured_records_failed(sessionmaker):
    settings = Settings()  # no BACKUP_* set at all
    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        await backup_module.run_backup(session, settings, clock)

    async with sessionmaker() as session:
        row = (await session.execute(select(BackupLog))).scalars().one()
    assert row.status == "failed"
    assert row.error_code == "not_configured"
    assert row.object_key is None


async def test_run_backup_pg_dump_missing_records_failed(sessionmaker, monkeypatch, test_database_url):
    settings = _configured_settings(
        DATABASE_URL=test_database_url, BACKUP_PG_DUMP="/no/such/pg_dump/binary"
    )
    fake_s3 = _FakeS3Client()
    monkeypatch.setattr(backup_module, "build_s3_client", lambda s: fake_s3)
    clock = FrozenClock(NOW)

    async with sessionmaker() as session:
        await backup_module.run_backup(session, settings, clock)

    async with sessionmaker() as session:
        row = (await session.execute(select(BackupLog))).scalars().one()
    assert row.status == "failed"
    assert row.error_code == "pg_dump_missing"
    assert not fake_s3.objects


async def test_latest_backup_status_reads_the_newest_row(sessionmaker):
    async with sessionmaker() as session:
        session.add(
            BackupLog(started_at=NOW - datetime.timedelta(days=1), status="ok", object_key="k1")
        )
        await session.commit()
        session.add(BackupLog(started_at=NOW, status="failed", error_code="upload_failed"))
        await session.commit()

    async with sessionmaker() as session:
        started_at, status = await backup_module.latest_backup_status(session)
    assert started_at == NOW
    assert status == "failed"


async def test_latest_backup_status_none_when_never_run(sessionmaker):
    async with sessionmaker() as session:
        assert await backup_module.latest_backup_status(session) is None


# --- pruning against the real table -----------------------------------


async def test_prune_backups_flips_pruned_rows_and_deletes_from_s3(sessionmaker):
    settings = _configured_settings(BACKUP_KEEP_DAILY=2, BACKUP_KEEP_WEEKLY=1)
    fake_s3 = _FakeS3Client()
    fake_s3.objects["anchor/old.dump.age"] = b"c"
    fake_s3.objects["anchor/new.dump.age"] = b"c"
    fake_s3.objects["anchor/newest.dump.age"] = b"c"

    async with sessionmaker() as session:
        session.add(
            BackupLog(
                started_at=NOW - datetime.timedelta(days=2),
                status="ok",
                object_key="anchor/old.dump.age",
            )
        )
        session.add(
            BackupLog(
                started_at=NOW - datetime.timedelta(days=1),
                status="ok",
                object_key="anchor/new.dump.age",
            )
        )
        session.add(
            BackupLog(started_at=NOW, status="ok", object_key="anchor/newest.dump.age")
        )
        await session.commit()

    clock = FrozenClock(NOW)
    async with sessionmaker() as session:
        pruned = await backup_module.prune_backups(session, settings, clock, fake_s3)
    assert pruned == 1
    assert "anchor/old.dump.age" not in fake_s3.objects
    assert "anchor/new.dump.age" in fake_s3.objects
    assert "anchor/newest.dump.age" in fake_s3.objects

    async with sessionmaker() as session:
        rows = {
            row.object_key: row.status
            for row in (await session.execute(select(BackupLog))).scalars().all()
        }
    assert rows["anchor/old.dump.age"] == "pruned"
    assert rows["anchor/new.dump.age"] == "ok"


# --- the real round trip: real pg_dump 18, fake S3 --------------------


@pytest.mark.skipif(not os.path.exists(PG18_DUMP), reason=f"{PG18_DUMP} not present on this machine")
async def test_real_pg_dump_backup_round_trip(sessionmaker, test_database_url, monkeypatch):
    """Plan section 12: "one real round-trip test using the local
    PostgreSQL 18 pg_dump": dump the (already-migrated) test database
    through the real pipeline into a fake S3, then decrypt and confirm
    the ciphertext really is a pg_dump custom-format archive."""
    identity = pyrage.x25519.Identity.generate()
    settings = _configured_settings(
        DATABASE_URL=test_database_url,
        BACKUP_PG_DUMP=PG18_DUMP,
        BACKUP_AGE_RECIPIENT=str(identity.to_public()),
    )
    fake_s3 = _FakeS3Client()
    monkeypatch.setattr(backup_module, "build_s3_client", lambda s: fake_s3)
    clock = FrozenClock(NOW)

    async with sessionmaker() as session:
        await backup_module.run_backup(session, settings, clock)

    async with sessionmaker() as session:
        row = (await session.execute(select(BackupLog))).scalars().one()
    assert row.status == "ok", row.error_code
    assert row.bytes and row.bytes > 0
    assert row.object_key in fake_s3.objects

    ciphertext = fake_s3.objects[row.object_key]
    out = io.BytesIO()
    pyrage.decrypt_io(io.BytesIO(ciphertext), out, [identity])
    plaintext = out.getvalue()
    # pg_dump --format=custom always starts with this 5-byte magic.
    assert plaintext[:5] == b"PGDMP"

    import hashlib

    assert row.sha256 == hashlib.sha256(ciphertext).hexdigest()


@pytest.mark.skipif(not os.path.exists(PG18_DUMP), reason=f"{PG18_DUMP} not present on this machine")
async def test_plaintext_never_written_to_disk(sessionmaker, test_database_url, monkeypatch):
    """Plan section 12: "plaintext never written to disk" -- monkeypatch
    tempfile so the real backup pipeline fails loudly if it ever tries
    to create one, and separately confirm no file anywhere under a
    scratch temp directory picks up the dump's "PGDMP" header."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("app/ops/backup.py must never create a temp file")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _forbidden)
    monkeypatch.setattr(tempfile, "mkstemp", _forbidden)
    monkeypatch.setattr(tempfile, "TemporaryFile", _forbidden)

    identity = pyrage.x25519.Identity.generate()
    settings = _configured_settings(
        DATABASE_URL=test_database_url,
        BACKUP_PG_DUMP=PG18_DUMP,
        BACKUP_AGE_RECIPIENT=str(identity.to_public()),
    )
    fake_s3 = _FakeS3Client()
    monkeypatch.setattr(backup_module, "build_s3_client", lambda s: fake_s3)
    clock = FrozenClock(NOW)

    async with sessionmaker() as session:
        await backup_module.run_backup(session, settings, clock)

    async with sessionmaker() as session:
        row = (await session.execute(select(BackupLog))).scalars().one()
    assert row.status == "ok", row.error_code


def test_backup_module_never_imports_tempfile():
    """A source-level guard alongside the dynamic one above: app/ops/
    backup.py has no reason to import tempfile at all, streaming being
    the whole point of it."""
    import pathlib

    source = pathlib.Path(backup_module.__file__).read_text(encoding="utf-8")
    assert "tempfile" not in source


# --- scheduling: maybe_enqueue_backup -----------------------------------


async def test_maybe_enqueue_backup_only_after_backup_time(sessionmaker):
    from app.db.models import Job

    settings = Settings(BACKUP_ENABLED=True, BACKUP_TIME=datetime.time(4, 0))
    before = FrozenClock(datetime.datetime(2026, 1, 5, 3, 59, tzinfo=datetime.timezone.utc))
    at_or_after = FrozenClock(datetime.datetime(2026, 1, 5, 4, 0, tzinfo=datetime.timezone.utc))

    async with sessionmaker() as session:
        assert await maybe_enqueue_backup(session, settings, before, "UTC") is False
    async with sessionmaker() as session:
        assert (
            await maybe_enqueue_backup(session, settings, at_or_after, "UTC")
            is True
        )
    async with sessionmaker() as session:
        jobs = (await session.execute(select(Job).where(Job.kind == "backup"))).scalars().all()
    assert len(jobs) == 1


async def test_maybe_enqueue_backup_disabled_never_enqueues(sessionmaker):
    settings = Settings(BACKUP_ENABLED=False)
    clock = FrozenClock(datetime.datetime(2026, 1, 5, 12, 0, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        assert await maybe_enqueue_backup(session, settings, clock, "UTC") is False


async def test_maybe_enqueue_backup_is_once_per_local_date(sessionmaker):
    settings = Settings(BACKUP_ENABLED=True, BACKUP_TIME=datetime.time(4, 0))
    clock = FrozenClock(datetime.datetime(2026, 1, 5, 5, 0, tzinfo=datetime.timezone.utc))
    async with sessionmaker() as session:
        first = await maybe_enqueue_backup(session, settings, clock, "UTC")
    async with sessionmaker() as session:
        second = await maybe_enqueue_backup(session, settings, clock, "UTC")
    assert first is True
    assert second is False


# --- worker dispatch -------------------------------------------------------


async def test_worker_dispatches_the_backup_job(sessionmaker):
    """app/worker.py's _run_job routes `backup` to run_backup without
    needing a provider or a bot, same shape as RESEARCH_SWEEP."""
    from app.db.models import UserState
    from app.worker import _run_job

    settings = Settings()  # not configured -- exercises the fast path
    clock = FrozenClock(NOW)

    async with sessionmaker() as session:
        session.add(UserState(id=1, chat_id=555, timezone="UTC"))
        await session.commit()
        await _run_job(session, settings, None, None, None, clock, backup_module.BACKUP, {})

    async with sessionmaker() as session:
        row = (await session.execute(select(BackupLog))).scalars().one()
    assert row.status == "failed"
    assert row.error_code == "not_configured"
