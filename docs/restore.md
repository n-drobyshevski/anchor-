# Restoring from a backup

Milestone 6e (Phase 6 plan section 9.2). Anchor's nightly backup
(`app/ops/backup.py`) is a `pg_dump --format=custom` archive, streamed
through [age](https://age-encryption.org/) encryption to
`BACKUP_AGE_RECIPIENT` and uploaded to
`s3://<bucket>/anchor/<YYYY>/<MM>/<DD>/anchor-<ts>.dump.age`. Only the
public age key ever touches the server -- restoring needs the matching
**private key**, which the user holds offline.

This is a manual runbook. `scripts/restore_check.py` automates the
mechanical steps against a local throwaway database, for a periodic
"does this still work" check -- it does not touch the live database and
is not a substitute for actually doing this once for real.

## What you need

- The `age` CLI (https://github.com/FiloSottile/age), or `pyrage` from
  a Python shell -- either decrypts the same format.
- The private key file generated with `age-keygen` when
  `BACKUP_AGE_RECIPIENT` was set up (`key.txt` below).
- `pg_restore` matching (or newer than) the backup's Postgres major
  version -- 18, in production.
- Access to the S3-compatible bucket (`BACKUP_S3_*` settings, or the
  Railway dashboard for the bucket's credentials).
- A target Postgres server to restore into. **Never restore into the
  live database in place** -- restore into a fresh one, verify it, then
  point `DATABASE_URL` at it.

## Steps

1. **Find the backup.** List objects under `anchor/<YYYY>/<MM>/<DD>/` in
   the bucket (the Railway dashboard, or `aws s3 ls
   s3://<bucket>/anchor/ --recursive --endpoint-url <BACKUP_S3_ENDPOINT>`
   with the bucket's access key). Pick the object you want -- usually
   the most recent `ok` row in `backup_log`, readable via `/state` or a
   direct query.

2. **Download it.**

   ```bash
   aws s3 cp s3://<bucket>/anchor/2026/09/23/anchor-20260923T040000Z.dump.age \
     ./anchor.dump.age --endpoint-url <BACKUP_S3_ENDPOINT>
   ```

3. **Decrypt it**, with the private key file (never commit or paste
   this key anywhere):

   ```bash
   age -d -i key.txt -o anchor.dump anchor.dump.age
   ```

4. **Create a fresh database** to restore into. Match production's
   locale (`C.UTF-8`) -- under a plain `C` locale, `pg_trgm` silently
   stops seeing Cyrillic, which would make memory retrieval look broken
   after a restore that actually succeeded:

   ```bash
   createdb --template=template0 --locale=C.UTF-8 --encoding=UTF8 anchor_restored
   ```

5. **Restore the dump:**

   ```bash
   pg_restore --no-owner --no-privileges -d anchor_restored anchor.dump
   ```

6. **Point `DATABASE_URL` at the restored database** (in `.env` for a
   local check, or the Railway service's variables for a real
   recovery):

   ```
   DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/anchor_restored
   ```

7. **Run migrations** -- a backup taken before the latest deploy may be
   behind `head`:

   ```bash
   uv run alembic upgrade head
   ```

8. **Smoke test.** At minimum:

   ```bash
   uv run python -c "
   import asyncio
   from app.db.session import create_engine_and_sessionmaker
   from sqlalchemy import text

   async def main():
       engine, sessionmaker = create_engine_and_sessionmaker('<the DATABASE_URL above>')
       async with sessionmaker() as session:
           for table in ('user_state', 'message', 'memory'):
               count = (await session.execute(text(f'select count(*) from {table}'))).scalar_one()
               print(table, count)
       await engine.dispose()

   asyncio.run(main())
   "
   ```

   For a real recovery, also start the app against the restored
   database in `polling` mode against a *test* bot token first, send it
   a message, and confirm `/state` and `/memories` look right before
   switching the production bot over.

## After a real recovery

- Rotate the age keypair if you suspect the private key was exposed
  during the incident (`docs/secrets.md`).
- Check `backup_log` in the restored database for the gap between the
  backup's `started_at` and the incident -- that is exactly what was
  lost.
