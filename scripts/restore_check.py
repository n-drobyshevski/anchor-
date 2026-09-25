#!/usr/bin/env python3
"""Manually run "does the backup actually restore" (Phase 6 plan
section 9.2; milestone 6e).

Downloads the latest (or a named) backup object using this process's
`BACKUP_S3_*` settings, decrypts it with a local age private key file,
restores it into a **fresh, local, throwaway** Postgres database --
never the live one -- and prints every table's row count.

The live database is not reachable from wherever this is run (a
laptop, typically), so there is nothing here to compare the counts
against -- this asserts the restore *worked* (pg_restore succeeded,
the schema exists, tables have rows) rather than that the numbers
match some other database. That is the honest scope of a script run
from outside the production network.

Usage:

    uv run python scripts/restore_check.py --key path/to/key.txt
    uv run python scripts/restore_check.py --key key.txt --object-key anchor/2026/09/23/anchor-....dump.age

`ANCHOR_ADMIN_DATABASE_URL` (default matching scripts/setup-postgres.sh:
postgresql://anchor:anchor@127.0.0.1:5433/postgres) is the admin
connection the throwaway database is created on and dropped from --
same pattern as eval/db.py's `throwaway_sessionmaker`, not reused
directly because that helper also runs alembic migrations, which a
restored-from-pg_dump database does not need (the dump already carries
the schema at whatever migration state it was backed up at).

Exit code 0 only when the restored database passes `verify()`:
pg_restore succeeded, `user_state` holds exactly one row (the bot's
singleton -- a dump without it is not a usable Anchor database), and
`alembic_version` names a revision this repo knows (so `alembic upgrade
head` can bring it forward). 1 otherwise. Comparing the counts against
the live database is deliberately out of scope: see docs/decisions.md.
The first successful run should be logged in the 6e milestone report,
per the plan.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import io
import os
import random
import string
import subprocess
import sys
import urllib.parse

import asyncpg
import boto3
import pyrage

from app.config import Settings

DEFAULT_ADMIN_URL = "postgresql://anchor:anchor@127.0.0.1:5433/postgres"

# The tables this check reports on, in the order a human reading the
# output would care about them. Not exhaustive -- pg_restore's own
# output already lists every table it created; this is a "does the
# stuff that matters look non-empty" summary.
KEY_TABLES = (
    "user_state",
    "message",
    "memory",
    "scene",
    "journal",
    "spend_ledger",
    "backup_log",
)


def _load_private_key(path: str) -> "pyrage.x25519.Identity":
    """Parse an age-keygen key file: the first non-comment, non-blank line."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                return pyrage.x25519.Identity.from_str(line)
    raise ValueError(f"{path} has no AGE-SECRET-KEY line")


def _s3_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.BACKUP_S3_ENDPOINT,
        aws_access_key_id=settings.BACKUP_S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.BACKUP_S3_SECRET_ACCESS_KEY,
        region_name=settings.BACKUP_S3_REGION,
    )


def _find_latest_object_key(client, bucket: str) -> str:
    """Object keys embed a sortable UTC timestamp
    (anchor/<Y>/<M>/<D>/anchor-<YYYYMMDDTHHMMSSZ>.dump.age), so the
    lexicographically greatest key under the prefix is the newest."""
    keys: list[str] = []
    continuation_token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": "anchor/"}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = client.list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")
    if not keys:
        raise SystemExit("no backup objects found under anchor/ in the bucket")
    return max(keys)


def _download(client, bucket: str, key: str) -> bytes:
    buf = io.BytesIO()
    client.download_fileobj(bucket, key, buf)
    return buf.getvalue()


def _decrypt(ciphertext: bytes, identity) -> bytes:
    return pyrage.decrypt(ciphertext, [identity])


def _admin_dsn(admin_url: str) -> str:
    return admin_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _libpq_parts(url: str) -> tuple[str, str, str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    user = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    dbname = parsed.path.lstrip("/")
    return user, password, host, port, dbname


async def _create_throwaway_db(admin_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{name}" TEMPLATE template0 LOCALE \'C.UTF-8\' ENCODING \'UTF8\'')
    finally:
        await conn.close()


async def _drop_throwaway_db(admin_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await conn.close()


def _pg_restore(plaintext: bytes, *, user: str, password: str, host: str, port: int, dbname: str) -> int:
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    proc = subprocess.run(
        [
            "pg_restore", "--no-owner", "--no-privileges",
            "-h", host, "-p", str(port), "-U", user, "-d", dbname,
        ],
        input=plaintext,
        env=env,
        capture_output=True,
    )
    if proc.returncode != 0:
        # pg_restore's stderr can carry table/row content on some
        # errors, but not connection strings -- unlike app/ops/
        # backup.py's pg_dump, this is a manual, local, operator-run
        # script, not something that ends up in shared logs, so this
        # one print is fine.
        sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))
    return proc.returncode


async def _report_row_counts(dsn: str) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        tables = [
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
        ]
        counts: dict[str, int] = {}
        for table in tables:
            counts[table] = await conn.fetchval(f'SELECT count(*) FROM "{table}"')
        return counts
    finally:
        await conn.close()


def known_revisions() -> set[str]:
    """Every revision id in this repo's migrations/ directory."""
    import pathlib

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = pathlib.Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return {script.revision for script in ScriptDirectory.from_config(cfg).walk_revisions()}


def verify(counts: dict[str, int], alembic_version: str | None, known: set[str]) -> list[str]:
    """The restore's pass/fail checks. Pure: returns what failed, [] if
    nothing did."""
    problems: list[str] = []
    if not counts:
        problems.append("the restore produced no tables at all")
    if counts.get("user_state") != 1:
        problems.append(f"user_state has {counts.get('user_state', 0)} rows, expected exactly 1")
    if alembic_version is None:
        problems.append("alembic_version is missing")
    elif alembic_version not in known:
        problems.append(f"alembic_version {alembic_version} is not a revision in this repo")
    return problems


async def _alembic_version(dsn: str) -> str | None:
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        if not exists:
            return None
        return await conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
    finally:
        await conn.close()


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    if not (settings.BACKUP_S3_ENDPOINT and settings.BACKUP_S3_BUCKET
            and settings.BACKUP_S3_ACCESS_KEY_ID and settings.BACKUP_S3_SECRET_ACCESS_KEY):
        print("BACKUP_S3_* settings are not configured in this environment.", file=sys.stderr)
        return 1

    identity = _load_private_key(args.key)
    client = _s3_client(settings)
    object_key = args.object_key or _find_latest_object_key(client, settings.BACKUP_S3_BUCKET)
    print(f"Restoring {object_key} ...")

    ciphertext = _download(client, settings.BACKUP_S3_BUCKET, object_key)
    plaintext = _decrypt(ciphertext, identity)
    print(f"Decrypted: {len(plaintext)} bytes")

    admin_dsn = _admin_dsn(args.admin_url)
    suffix = "".join(random.choices(string.ascii_lowercase, k=8))
    db_name = f"anchor_restore_check_{suffix}"
    await _create_throwaway_db(admin_dsn, db_name)
    try:
        user, password, host, port, _ = _libpq_parts(admin_dsn)
        returncode = _pg_restore(
            plaintext, user=user, password=password, host=host, port=port, dbname=db_name
        )
        if returncode != 0:
            print(f"pg_restore exited {returncode}", file=sys.stderr)
            return 1

        target_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"
        counts = await _report_row_counts(target_dsn)
        version = await _alembic_version(target_dsn)

        print(f"Restored {len(counts)} tables:")
        for table in KEY_TABLES:
            if table in counts:
                print(f"  {table}: {counts[table]}")
        others = sorted(set(counts) - set(KEY_TABLES))
        if others:
            print(f"  (+{len(others)} more tables)")

        print(f"  alembic_version: {version}")
        problems = verify(counts, version, known_revisions())
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"Restore check finished at {datetime.datetime.now(datetime.UTC).isoformat()}")
        return 0
    finally:
        await _drop_throwaway_db(admin_dsn, db_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="path to the age private key file")
    parser.add_argument("--object-key", default=None, help="a specific backup object; default: latest")
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("ANCHOR_ADMIN_DATABASE_URL", DEFAULT_ADMIN_URL),
        help="admin Postgres connection the throwaway database is created on",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
