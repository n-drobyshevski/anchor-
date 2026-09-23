"""scripts/restore_check.py -- the manual "does the backup actually
restore" check (Phase 6 plan section 9.2; milestone 6e).

One end-to-end test: a real pg_dump of the (already-migrated) test
database, age-encrypted with a test keypair, "uploaded" to a fake
in-memory S3, then run through the script's own `main_async` exactly
as the CLI would -- restored into a fresh local throwaway database and
its row counts printed. Skipped with a clear reason when the local
Postgres 18 pg_dump/pg_restore binaries are not present.

This is also the "first successful run", logged in the 6e milestone
report, that plan section 9.2 asks for -- against a fake bucket rather
than a real Railway one, since no real bucket credentials are available
in this environment. See the milestone report for what that means the
user still needs to verify for real.
"""

from __future__ import annotations

import argparse
import os
import subprocess

import pyrage
import pytest

from app.config import Settings

pytestmark = pytest.mark.skipif(
    not (os.path.exists("/usr/lib/postgresql/18/bin/pg_dump") and os.path.exists(
        "/usr/lib/postgresql/18/bin/pg_restore"
    )),
    reason="local PostgreSQL 18 pg_dump/pg_restore not present on this machine",
)

PG_DUMP = "/usr/lib/postgresql/18/bin/pg_dump"
PG_RESTORE = "/usr/lib/postgresql/18/bin/pg_restore"


def _libpq_target(database_url: str):
    import urllib.parse

    raw = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urllib.parse.urlsplit(raw)
    return (
        parsed.username or "",
        parsed.password or "",
        parsed.hostname or "127.0.0.1",
        parsed.port or 5432,
        parsed.path.lstrip("/"),
    )


class _FakeS3Client:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        matching = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in matching], "IsTruncated": False}

    def download_fileobj(self, bucket, key, buf):
        buf.write(self.objects[key])


async def test_restore_check_end_to_end(test_database_url, tmp_path, monkeypatch):
    from scripts import restore_check

    # 1. A real pg_dump of the test database.
    user, password, host, port, dbname = _libpq_target(test_database_url)
    env = dict(os.environ)
    env["PGPASSWORD"] = password
    dump = subprocess.run(
        [PG_DUMP, "--format=custom", "--no-password", "-h", host, "-p", str(port), "-U", user, dbname],
        env=env, capture_output=True, check=True,
    ).stdout
    assert dump[:5] == b"PGDMP"

    # 2. Encrypt it with a test age keypair; write the private key to a
    # local file the script reads exactly as a real one would be.
    identity = pyrage.x25519.Identity.generate()
    key_path = tmp_path / "key.txt"
    key_path.write_text(f"# public key: {identity.to_public()}\n{identity}\n")

    ciphertext = pyrage.encrypt(dump, [identity.to_public()])

    # 3. Put it in a fake bucket, monkeypatch the script's S3 client
    # factory to hand back that fake instead of a real boto3 client.
    object_key = "anchor/2026/01/01/anchor-20260101T040000Z.dump.age"
    fake_s3 = _FakeS3Client({object_key: ciphertext})
    monkeypatch.setattr(restore_check, "_s3_client", lambda settings: fake_s3)
    monkeypatch.setattr(
        restore_check,
        "Settings",
        lambda: Settings(
            BACKUP_S3_ENDPOINT="https://fake.example",
            BACKUP_S3_BUCKET="anchor-backups",
            BACKUP_S3_ACCESS_KEY_ID="k",
            BACKUP_S3_SECRET_ACCESS_KEY="s",
        ),
    )

    admin_dsn = f"postgresql://{user}:{password}@{host}:{port}/postgres"
    args = argparse.Namespace(key=str(key_path), object_key=object_key, admin_url=admin_dsn)

    # 4. Run the script's own main_async, exactly as the CLI would.
    exit_code = await restore_check.main_async(args)
    assert exit_code == 0


def test_load_private_key_skips_comments_and_blank_lines(tmp_path):
    from scripts import restore_check

    identity = pyrage.x25519.Identity.generate()
    key_path = tmp_path / "key.txt"
    key_path.write_text(f"# created: 2026-01-01\n# public key: {identity.to_public()}\n\n{identity}\n")

    loaded = restore_check._load_private_key(str(key_path))
    assert str(loaded.to_public()) == str(identity.to_public())


def test_find_latest_object_key_picks_the_lexicographically_greatest():
    from scripts import restore_check

    client = _FakeS3Client(
        {
            "anchor/2026/01/01/anchor-20260101T040000Z.dump.age": b"a",
            "anchor/2026/01/03/anchor-20260103T040000Z.dump.age": b"c",
            "anchor/2026/01/02/anchor-20260102T040000Z.dump.age": b"b",
        }
    )
    assert (
        restore_check._find_latest_object_key(client, "bucket")
        == "anchor/2026/01/03/anchor-20260103T040000Z.dump.age"
    )
