"""Encrypted nightly Postgres backups to an S3-compatible bucket (Phase 6
plan section 9.1; milestone 6e).

**Not idle work.** This job kind is enqueued and dispatched exactly
like `research_sweep`/`notebook_expiry` (app/core/scheduler.py's
`maybe_enqueue_backup`, app/worker.py's `_heartbeat_loop`) -- once a
local day, at `BACKUP_TIME` or later, deduplicated on the local date.
It is never gated on `IDLE_ENABLED`, the idle budget, the idle window
or `user_state.persona_active`: a backup is infrastructure, not a
courtesy to a person who might be asleep, and it must run even on a
day the idle framework's own reserve is fully spent or the persona is
paused.

**Streaming, no plaintext on disk.** `pyrage.encrypt_io(reader, writer,
recipients)` (confirmed empirically: it calls `reader.read()` in
bounded chunks and `writer.write()` per chunk, not a single
read-everything-then-encrypt call -- see this module's own tests) is
handed `pg_dump`'s live subprocess stdout as `reader` directly, and a
`_S3MultipartWriter` as `writer`, so the pipeline is genuinely
pg_dump -> age encryption -> S3 multipart upload, streamed end to end.
**The only thing ever held in memory is the writer's current multipart
part** (`_MULTIPART_PART_SIZE`, 8 MiB) -- never the whole dump. This is
true streaming, not "buffer it all in memory instead of on disk"; the
one caveat is that pyrage's Rust internals are opaque from here, so
this module's own test asserts the *observable* contract (bounded
`write()` chunk sizes, no temp file, no path under the dump's own
"PGDMP" header ever created) rather than pyrage's implementation.

**The private key never touches the server.** Only `BACKUP_AGE_RECIPIENT`
(a public key) is configured here; decryption happens offline, per
docs/restore.md.

**Password handling.** `_libpq_target` splits `DATABASE_URL` into its
libpq parts and the password travels only via the `PGPASSWORD`
environment variable of the `pg_dump` subprocess -- never on argv,
where it would appear in `ps`, and never logged.

**Failure reporting.** Every `backup_log` row this module writes on
failure carries a bare `error_code` (`not_configured`, `pg_dump_missing`,
`pg_dump_failed`, `encrypt_failed`, `upload_failed`) and nothing else --
no stderr text, no URL, no exception message. `pg_dump`'s stderr is
read to completion (so the pipe cannot deadlock) and discarded; only
its byte length ever reaches a log line, via `SAFE_EXTRA_KEYS`.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import os
import subprocess
import urllib.parse

import boto3
import pyrage
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.clock import Clock
from app.db.models import BackupLog

logger = logging.getLogger(__name__)

BACKUP = "backup"

OK = "ok"
FAILED = "failed"
PRUNED = "pruned"

NOT_CONFIGURED = "not_configured"
PG_DUMP_MISSING = "pg_dump_missing"
PG_DUMP_FAILED = "pg_dump_failed"
ENCRYPT_FAILED = "encrypt_failed"
UPLOAD_FAILED = "upload_failed"
EMPTY_DUMP = "empty_dump"

_PREFIX = "anchor"
# S3's own minimum part size is 5 MiB (the last part may be smaller);
# 8 MiB keeps comfortably above that while still bounding the writer's
# in-memory buffer to a small, fixed size regardless of dump size.
_MULTIPART_PART_SIZE = 8 * 1024 * 1024


def backup_dedup_key(local_date: datetime.date) -> str:
    """One backup per local date, ever -- mirrors research_sweep_dedup_key."""
    return f"backup:{local_date.isoformat()}"


class BackupError(Exception):
    """Carries only a bare error code -- never a message with content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _libpq_target(database_url: str) -> tuple[str, str, str, int, str]:
    """(user, password, host, port, dbname) from an asyncpg or plain URL.

    The password is returned only so the caller can put it in the
    subprocess environment (`PGPASSWORD`) -- it must never be placed on
    a command line or logged.
    """
    raw = database_url
    if raw.startswith("postgresql+asyncpg://"):
        raw = "postgresql://" + raw[len("postgresql+asyncpg://") :]
    parsed = urllib.parse.urlsplit(raw)
    user = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/")
    return user, password, host, port, dbname


def object_key(now: datetime.datetime) -> str:
    """`anchor/<YYYY>/<MM>/<DD>/anchor-<ts>.dump.age` (plan section 9.1)."""
    return f"{_PREFIX}/{now:%Y}/{now:%m}/{now:%d}/anchor-{now:%Y%m%dT%H%M%SZ}.dump.age"


def is_configured(settings: Settings) -> bool:
    return bool(
        settings.BACKUP_AGE_RECIPIENT
        and settings.BACKUP_S3_ENDPOINT
        and settings.BACKUP_S3_BUCKET
        and settings.BACKUP_S3_ACCESS_KEY_ID
        and settings.BACKUP_S3_SECRET_ACCESS_KEY
    )


def build_s3_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.BACKUP_S3_ENDPOINT,
        aws_access_key_id=settings.BACKUP_S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.BACKUP_S3_SECRET_ACCESS_KEY,
        region_name=settings.BACKUP_S3_REGION,
    )


class _S3WriteError(Exception):
    """An upload_part/create/complete call failed -- distinguished from
    an age-encryption failure so backup_log gets the right error_code."""


class _CountingReader:
    """Wraps `pg_dump`'s stdout and counts plaintext bytes read.

    age's own stream format always writes a header and a MAC, even for
    a zero-byte plaintext, so the *ciphertext* the writer sees is never
    empty -- `_S3MultipartWriter.total_bytes` cannot tell "pg_dump
    produced nothing" from "pg_dump produced a tiny dump". This wrapper
    is how `_run_pipeline_sync` tells the two apart, on the plaintext
    side, without ever holding more than one `read()` call's worth of
    bytes at a time.
    """

    def __init__(self, fileobj) -> None:
        self._fileobj = fileobj
        self.total_bytes = 0

    def read(self, n=-1) -> bytes:
        chunk = self._fileobj.read(n)
        self.total_bytes += len(chunk)
        return chunk


class _S3MultipartWriter:
    """A write()-only file object streaming into one S3 multipart upload.

    Bytes handed to `write()` are buffered only up to one part
    (`_MULTIPART_PART_SIZE`) before being flushed with `upload_part` --
    the ciphertext is never held in full, on disk or in memory.
    sha256 is updated incrementally as bytes arrive.
    """

    def __init__(self, client, bucket: str, key: str) -> None:
        self._client = client
        self._bucket = bucket
        self._key = key
        self._buffer = bytearray()
        self._parts: list[dict] = []
        self._upload_id: str | None = None
        self._sha256 = hashlib.sha256()
        self._total_bytes = 0
        self._part_sizes: list[int] = []  # test hook: every flushed chunk size

    def _ensure_started(self) -> None:
        if self._upload_id is None:
            try:
                resp = self._client.create_multipart_upload(Bucket=self._bucket, Key=self._key)
            except (ClientError, BotoCoreError) as exc:
                raise _S3WriteError from exc
            self._upload_id = resp["UploadId"]

    def _flush_part(self, *, final: bool) -> None:
        if not self._buffer:
            return
        if not final and len(self._buffer) < _MULTIPART_PART_SIZE:
            return
        self._ensure_started()
        part_number = len(self._parts) + 1
        body = bytes(self._buffer)
        try:
            resp = self._client.upload_part(
                Bucket=self._bucket,
                Key=self._key,
                PartNumber=part_number,
                UploadId=self._upload_id,
                Body=body,
            )
        except (ClientError, BotoCoreError) as exc:
            raise _S3WriteError from exc
        self._parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
        self._part_sizes.append(len(body))
        self._buffer.clear()

    def write(self, data) -> int:
        data = bytes(data)
        self._sha256.update(data)
        self._total_bytes += len(data)
        self._buffer.extend(data)
        self._flush_part(final=False)
        return len(data)

    def close(self) -> None:
        if self._total_bytes == 0:
            return
        self._flush_part(final=True)
        try:
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=self._key,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
        except (ClientError, BotoCoreError) as exc:
            raise _S3WriteError from exc

    def abort(self) -> None:
        if self._upload_id is not None:
            try:
                self._client.abort_multipart_upload(
                    Bucket=self._bucket, Key=self._key, UploadId=self._upload_id
                )
            except Exception:  # noqa: BLE001 - best-effort cleanup only
                pass

    @property
    def sha256_hex(self) -> str:
        return self._sha256.hexdigest()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes


def _run_pipeline_sync(
    settings: Settings, s3_client, key: str
) -> tuple[int, str]:
    """Blocking: pg_dump | age-encrypt | multipart upload, fully streamed.

    Called from `run_backup` via `asyncio.to_thread` -- everything here
    is synchronous (subprocess, pyrage, boto3), so it must not run on
    the event loop. Raises BackupError with a bare code on any failure
    and leaves no partial object on the bucket (the multipart upload is
    aborted rather than completed).
    """
    try:
        recipient = pyrage.x25519.Recipient.from_str(settings.BACKUP_AGE_RECIPIENT)
    except Exception as exc:  # noqa: BLE001 - pyrage.RecipientError, malformed key
        raise BackupError(NOT_CONFIGURED) from exc

    user, password, host, port, dbname = _libpq_target(settings.DATABASE_URL)
    pg_dump_bin = settings.BACKUP_PG_DUMP or "pg_dump"

    env = dict(os.environ)
    env["PGPASSWORD"] = password

    cmd = [
        pg_dump_bin,
        "--format=custom",
        "--no-password",
        "-h", host,
        "-p", str(port),
        "-U", user,
        dbname,
    ]

    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
    except FileNotFoundError as exc:
        raise BackupError(PG_DUMP_MISSING) from exc

    writer = _S3MultipartWriter(s3_client, settings.BACKUP_S3_BUCKET, key)
    assert proc.stdout is not None
    reader = _CountingReader(proc.stdout)
    try:
        pyrage.encrypt_io(reader, writer, [recipient])
    except _S3WriteError as exc:
        proc.kill()
        proc.wait()
        writer.abort()
        raise BackupError(UPLOAD_FAILED) from exc
    except Exception as exc:  # noqa: BLE001 - age encryption/stream failure
        proc.kill()
        proc.wait()
        writer.abort()
        raise BackupError(ENCRYPT_FAILED) from exc
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    returncode = proc.wait()
    # Read to completion so the pipe cannot deadlock; only the byte
    # length is ever used (and only for a log line), never the content
    # -- pg_dump's stderr can carry the connection string it was given.
    stderr_len = len(proc.stderr.read()) if proc.stderr else 0
    if proc.stderr is not None:
        proc.stderr.close()
    logger.info("pg_dump finished", extra={"event": "pg_dump", "count": stderr_len})

    if returncode != 0:
        writer.abort()
        raise BackupError(PG_DUMP_FAILED)

    if reader.total_bytes == 0:
        writer.abort()
        raise BackupError(EMPTY_DUMP)

    try:
        writer.close()
    except _S3WriteError as exc:
        raise BackupError(UPLOAD_FAILED) from exc

    return writer.total_bytes, writer.sha256_hex


async def run_backup(session: AsyncSession, settings: Settings, clock: Clock) -> None:
    """The nightly backup job (job kind `backup`, dispatched by app/worker.py).

    One `backup_log` row per attempt: `status='ok'` with `object_key`,
    `bytes`, `sha256` on success; `status='failed'` with `error_code`
    otherwise. Never raises -- a backup failure must not fail the job
    loudly in a way that could look like an inbound-processing bug, and
    /state (app/tg/router.py's `latest_backup_status`) is how the user
    actually learns about it.
    """
    started_at = clock.now_utc()

    if not is_configured(settings):
        session.add(
            BackupLog(
                started_at=started_at,
                finished_at=clock.now_utc(),
                status=FAILED,
                error_code=NOT_CONFIGURED,
            )
        )
        await session.commit()
        logger.warning("backup not configured", extra={"event": BACKUP, "error_code": NOT_CONFIGURED})
        return

    key = object_key(started_at)
    s3_client = build_s3_client(settings)
    try:
        bytes_written, sha256_hex = await _run_pipeline_in_thread(settings, s3_client, key)
    except BackupError as exc:
        session.add(
            BackupLog(
                started_at=started_at,
                finished_at=clock.now_utc(),
                status=FAILED,
                error_code=exc.code,
            )
        )
        await session.commit()
        logger.warning("backup failed", extra={"event": BACKUP, "error_code": exc.code})
        return

    session.add(
        BackupLog(
            started_at=started_at,
            finished_at=clock.now_utc(),
            object_key=key,
            bytes=bytes_written,
            sha256=sha256_hex,
            status=OK,
        )
    )
    await session.commit()
    logger.info("backup done", extra={"event": BACKUP, "bytes": bytes_written})

    await prune_backups(session, settings, clock, s3_client)


async def _run_pipeline_in_thread(settings: Settings, s3_client, key: str) -> tuple[int, str]:
    return await asyncio.to_thread(_run_pipeline_sync, settings, s3_client, key)


def select_prune_targets(
    entries: list[tuple[int, str, datetime.date]],
    keep_daily: int,
    keep_weekly: int,
) -> list[int]:
    """Which `backup_log` ids to prune: keep the `keep_daily` most recent
    backups (any weekday) plus the `keep_weekly` most recent Sunday
    (local) backups; return the ids of the rest.

    A pure function over `(id, object_key, local_date)` triples, one per
    successful backup, so pruning policy is unit-testable without a
    database or a bucket.
    """
    by_date = sorted(entries, key=lambda e: e[2], reverse=True)
    keep: set[int] = set()
    for row_id, _key, _date in by_date[:keep_daily]:
        keep.add(row_id)
    sundays = [e for e in by_date if e[2].isoweekday() == 7]
    for row_id, _key, _date in sundays[:keep_weekly]:
        keep.add(row_id)
    return [row_id for row_id, _key, _date in by_date if row_id not in keep]


async def prune_backups(
    session: AsyncSession, settings: Settings, clock: Clock, s3_client
) -> int:
    """Delete every `ok` backup object outside the keep window, log
    `pruned` (plan section 9.1). Returns the count pruned.

    Runs after every successful backup, same cadence-follows-the-backup
    shape as app/research/sweeps.py's two sweeps sharing one job.
    """
    result = await session.execute(
        select(BackupLog.id, BackupLog.object_key, BackupLog.started_at)
        .where(BackupLog.status == OK)
        .where(BackupLog.object_key.is_not(None))
    )
    entries = [
        (row.id, row.object_key, row.started_at.date()) for row in result.all()
    ]
    targets = select_prune_targets(entries, settings.BACKUP_KEEP_DAILY, settings.BACKUP_KEEP_WEEKLY)
    if not targets:
        return 0

    by_id = {row_id: key for row_id, key, _date in entries}
    for row_id in targets:
        key = by_id[row_id]
        await asyncio.to_thread(_delete_object, s3_client, settings.BACKUP_S3_BUCKET, key)
        result_row = await session.get(BackupLog, row_id)
        if result_row is not None:
            result_row.status = PRUNED
    await session.commit()
    logger.info("backups pruned", extra={"event": "backup_prune", "count": len(targets)})
    return len(targets)


def _delete_object(s3_client, bucket: str, key: str) -> None:
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
    except (ClientError, BotoCoreError):
        # Best-effort: a bucket-side object already gone (or a transient
        # error) must not fail the whole prune pass, which would leave
        # every later, still-prunable object stuck too. The backup_log
        # row still flips to 'pruned' -- see prune_backups above -- so a
        # genuinely failed delete is at worst an orphan object, never a
        # crash of nightly housekeeping.
        pass


def _list_all_objects(s3_client, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    continuation_token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = s3_client.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")
    return keys


def _purge_all_sync(settings: Settings) -> int:
    """Blocking: delete every object under the `anchor/` prefix.

    Returns the count deleted. A no-op (0) if S3 is not configured --
    /delete must still proceed with the database wipe in that case
    (plan section 9.3's own "if S3 isn't configured, the wipe still
    proceeds").
    """
    if not (
        settings.BACKUP_S3_ENDPOINT
        and settings.BACKUP_S3_BUCKET
        and settings.BACKUP_S3_ACCESS_KEY_ID
        and settings.BACKUP_S3_SECRET_ACCESS_KEY
    ):
        return 0

    s3_client = build_s3_client(settings)
    keys = _list_all_objects(s3_client, settings.BACKUP_S3_BUCKET, f"{_PREFIX}/")
    deleted = 0
    for batch_start in range(0, len(keys), 1000):
        batch = keys[batch_start : batch_start + 1000]
        try:
            s3_client.delete_objects(
                Bucket=settings.BACKUP_S3_BUCKET,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )
        except (ClientError, BotoCoreError):
            continue
        deleted += len(batch)
    return deleted


async def purge_all_backups(settings: Settings) -> int:
    """/delete's S3 side (plan section 9.3): purge every backup object
    under the `anchor/` prefix. Returns the count deleted, for a log
    line only -- never the keys themselves."""
    return await asyncio.to_thread(_purge_all_sync, settings)


async def latest_backup_status(
    session: AsyncSession,
) -> tuple[datetime.datetime, str] | None:
    """`(started_at, status)` of the most recent backup attempt, or None
    if none has ever run -- /state's "Бэкап: ..." line (app/tg/router.py)
    needs the full timestamp for its success message
    ("Бэкап: <дата время> ок"), not just the date.

    `status` is `ok`/`failed`/`pruned` straight from the row: /state
    only distinguishes ok-ish (not failed) from failed, per plan section
    9.1's two message shapes, but the raw value is kept here so a test
    can assert on it directly.
    """
    result = await session.execute(
        select(BackupLog.started_at, BackupLog.status)
        .order_by(BackupLog.id.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    started_at, status = row
    return started_at, status
