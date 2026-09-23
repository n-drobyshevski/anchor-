# Secrets rotation runbook

Milestone 6e (Phase 6 plan section 9.6). Every credential this app
holds, where it lives, and how to rotate it without downtime (except
where noted).

## Telegram bot token (`TELEGRAM_BOT_TOKEN`)

1. In BotFather: `/mybots` -> the bot -> API Token -> Revoke current
   token.
2. Copy the new token BotFather issues.
3. Set `TELEGRAM_BOT_TOKEN` to it in the Railway service's variables.
4. Redeploy. `app/main.py` calls `bot.set_webhook(...)` again on
   startup, so the new token is live as soon as the new deploy is.
5. The old token stops working the instant BotFather revokes it, so
   there is a short window (however long the redeploy takes) where the
   bot cannot receive updates. Telegram queues undelivered webhook
   updates for a while and retries, so a redeploy of a minute or two
   does not lose messages.

## OpenRouter API key (`OPENROUTER_API_KEY`)

1. https://openrouter.ai/settings/keys -> create a new key.
2. Set `OPENROUTER_API_KEY` to it in Railway's variables, redeploy.
3. Once the new deploy is confirmed working (a `/state` reply, or
   `scripts/smoke.py` against a throwaway config), revoke the old key
   on OpenRouter's dashboard.
4. No downtime: both keys are valid during the overlap.

## S3 keys (`BACKUP_S3_ACCESS_KEY_ID` / `BACKUP_S3_SECRET_ACCESS_KEY`)

The Railway bucket's credentials.

1. In the Railway bucket's settings, generate a new access key/secret
   pair (or regenerate, if the plugin only offers one).
2. Set both `BACKUP_S3_ACCESS_KEY_ID` and `BACKUP_S3_SECRET_ACCESS_KEY`
   in the service's variables, redeploy.
3. Confirm the next nightly backup (or a manually fired one) records
   `backup_log.status='ok'` -- `/state`'s "Бэкап: ..." line, or a direct
   query.
4. Revoke the old key pair once a backup has succeeded on the new one.
5. This has no bearing on decrypting *existing* backups -- those are
   encrypted with the age keypair below, entirely separate from the S3
   credentials that only gate upload/download access to the bucket.

## The Telegram webhook secret (`TELEGRAM_SECRET_TOKEN`)

1. Generate a new one: `openssl rand -hex 32` (must match
   `^[A-Za-z0-9_-]{1,256}$`, which hex output always does).
2. Set `TELEGRAM_SECRET_TOKEN` in Railway's variables, redeploy.
   `app/main.py` re-registers the webhook with the new secret on
   startup (`bot.set_webhook(..., secret_token=...)`), so Telegram
   starts sending the new header immediately.
3. There is a brief window during the redeploy where in-flight
   requests still carry the old secret; `app/tg/webhook.py`'s
   `verify_secret` rejects those with 403 until the new deploy is live,
   and Telegram retries.

## The age keypair (`BACKUP_AGE_RECIPIENT` and the offline private key)

The one credential that is **never** in an environment variable on the
server -- only the public half (`BACKUP_AGE_RECIPIENT`) is.

1. Generate a new keypair offline: `age-keygen -o new-key.txt`. The
   file's `# public key:` comment line is the new recipient.
2. Set `BACKUP_AGE_RECIPIENT` to the new public key in Railway's
   variables, redeploy.
3. **Every backup taken before this point can still only be decrypted
   with the *old* private key.** Keep the old key file somewhere safe
   until you are certain you will never need to restore from a
   pre-rotation backup (the retention window: up to 14 daily + 8 weekly
   backups, so roughly two months at the default settings).
4. Store the new private key file (`new-key.txt`) offline, the same way
   the original was kept -- never in the repo, never in an environment
   variable, never in a chat message.
5. Rotate this one whenever you suspect the private key file was
   exposed (a compromised laptop, an accidental commit caught before
   push, etc.) -- a leaked *server* or *bucket* does not expose backup
   contents on its own, by design (`app/ops/backup.py`'s module
   docstring), but a leaked private key does.

## General notes

- Rotate a credential the moment you suspect it leaked, not on a
  schedule alone.
- Never paste a secret into a commit, a Telegram message to the bot, or
  this repo's docs -- `.gitleaks.toml` and CI's `gitleaks` job exist to
  catch exactly that mistake, but the first line of defense is not
  typing it there at all.
- After rotating anything, confirm the app actually came back up
  (`/healthz`, `/readyz`, or a `/state` reply) before considering the
  rotation done.
