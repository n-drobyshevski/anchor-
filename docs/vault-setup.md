# Setting up the vault service (milestone 5a)

You do these steps once, by hand. Claude Code never does them: it may
not read the vault or hold its credentials (CLAUDE.md). At the end,
`/vault` in Telegram tells you the sync is running. Until 5b nothing
appears in your vault: 5a only proves the pipe works.

## What you need

- An Obsidian Sync subscription. Standard gives 1 synced vault, 1 GB,
  and one month of version history; Plus gives twelve months. The only
  thing that depends on which one you have is a line in `/delete`'s
  confirmation, which arrives in 5b.
- Desktop Obsidian, to create the vault.
- Node.js 22 or later on your own machine, for one login command.

## 1. Create the remote vault, end-to-end encrypted

In desktop Obsidian, create a new vault. Then open **Settings → Sync →
Remote vault → Create new vault**, pick **End-to-end encryption**, and
choose a password. Keep that password: it is `OBSIDIAN_E2EE_PASSWORD`
below.

Use a vault you **own**. vaultd refuses to start on a vault that is
shared with your account, because a collaborator on a shared vault
could write Anchor's facts.

## 2. Get the auth token

On your own machine:

```bash
npx obsidian-headless@0.0.14 login
npx obsidian-headless@0.0.14 sync-list-remote
```

The first command logs you in. The second lists your vaults under
`Vaults:` (yours) and `Shared vaults:` (others'). Note the **id** of
the vault from step 1, the long string before its name. Use it as
`OBSIDIAN_VAULT`: an id cannot become ambiguous the way a name can.

Then copy the token that `login` stored:

- Linux: `~/.config/obsidian-headless/auth_token`
- macOS and Windows: `~/.obsidian-headless/auth_token`

**That token grants full access to your Obsidian account.** It goes
into the vault service's Railway variables and nowhere else: not the
bot's service, not a chat, not this repo.

## 3. Create the `vault` service on Railway

In the Anchor project, create a service from this GitHub repo and name
it **`vault`**. The name matters: the bot reaches it at
`vault.railway.internal`, which is `VAULT_URL`'s default.

**Settings.** These live in the dashboard. Railway has deprecated
`railway.json` (Config as Code), and new services cannot use it, so
there is no file in the repo for them (docs/decisions.md):

| Setting | Value |
|---|---|
| Source → Root Directory | `/vaultd` |
| Build → Builder | Dockerfile (picked up from `vaultd/Dockerfile`) |
| Build → Watch Paths | `/vaultd/**` |
| Deploy → Healthcheck Path | `/healthz` |
| Deploy → Restart Policy | Always |
| Deploy → Replicas | 1 (a service with a volume cannot have more) |
| Networking → Public domain | **none. Do not generate one.** |
| Networking → TCP proxy | **none** |

vaultd refuses to start if the service has a public domain or a TCP
proxy (`RAILWAY_PUBLIC_DOMAIN` or `RAILWAY_TCP_PROXY_DOMAIN` set). Its
API is meant for the private network only.

**Volume.** Attach a volume to this service at `/data`. It holds the
whole vault, attachments included (obsidian-headless cannot leave them
out), plus ob's sync state. Size it for your vault; the Standard plan
caps a vault at 1 GB.

**Variables:**

| Variable | Value |
|---|---|
| `VAULT_API_TOKEN` | at least 32 random characters, e.g. `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` |
| `OBSIDIAN_AUTH_TOKEN` | the token from step 2 |
| `OBSIDIAN_VAULT` | the vault id from step 2 |
| `OBSIDIAN_E2EE_PASSWORD` | the password from step 1 |
| `OB_DEVICE_NAME` | `anchor-railway` (shows in Sync's version history) |
| `VAULT_PATH` | `/data/vault` |
| `XDG_CONFIG_HOME` | `/data/config` |
| `PORT` | `8080` |

## 4. Point the bot at it

On the bot's service:

| Variable | Value |
|---|---|
| `VAULT_API_TOKEN` | the same value, e.g. `${{vault.VAULT_API_TOKEN}}` |
| `VAULT_MODE` | `status` |
| `VAULT_URL` | leave unset (defaults to `http://vault.railway.internal:8080`) |

The bot checks `VAULT_URL` at boot and refuses anything except `http://`
to a `*.railway.internal` host, with no path and no credentials. It
never prints the value.

## 5. Check that it works

The vault service's deploy logs should show, in order: `ob
sync-setup done` (first boot only), `boot done`, `ob sync started`.
They never show a file name, a path or the vault's name. `ob`'s own
output is discarded.

In Telegram:

- `/vault` → `Хранилище: синхронизация ок (работает с 14:02, перезапусков 0).`
- `/state` → a line `Хранилище: ок`.

Other answers, and what they mean:

| `/vault` says | Meaning |
|---|---|
| `Хранилище выключено.` | `VAULT_MODE` is `off` on the bot. |
| `Хранилище: нет связи с сервисом хранилища.` | The bot cannot reach the vault service. Check that the service is named `vault`, is running, and has `PORT=8080`. |
| `Хранилище: сервис отказал в доступе — …` | `VAULT_API_TOKEN` differs between the two services. |
| `Хранилище: синхронизация остановлена (перезапусков N, код выхода C).` | vaultd is up, but `ob sync` keeps exiting. It is restarted with backoff from 5 s to 5 min. |

## If the vault service refuses to start

It exits with a message that names a variable, never its value:

| Message starts with | Fix |
|---|---|
| `RAILWAY_PUBLIC_DOMAIN is set` / `RAILWAY_TCP_PROXY_DOMAIN is set` | Remove the domain or TCP proxy from the service. |
| `VAULT_API_TOKEN must be at least 32 characters` | Use a longer token, on both services. |
| `Missing required environment variables: …` | Set the `OBSIDIAN_*` variables it names. |
| `ob sync-list-remote failed` | `OBSIDIAN_AUTH_TOKEN` is wrong or expired. Repeat step 2. |
| `OBSIDIAN_VAULT names a vault shared with this account` | Use a vault you own. If a shared vault has the same name as yours, use your vault's id. |
| `OBSIDIAN_VAULT matches no vault owned by this account` | Check the id with `sync-list-remote`. |
| `VAULT_PATH is already linked to a different remote vault` | `OBSIDIAN_VAULT` changed after the first boot. If that is intended, delete `/data/config/obsidian-headless/sync/` on the volume and redeploy. |
| `ob sync-setup failed (exit 2)` | `OBSIDIAN_E2EE_PASSWORD` is wrong. |

## 6. Mirror: your facts in Obsidian (5b)

Set `VAULT_MODE=mirror` on the bot. Within a minute, facts start to
appear in `Anchor/Memory/` and days in `Anchor/Journal/`, 50 files a
minute until everything is there. `/vault` then says `· фактов N`.

Copy `docs/vault/Memory.base` into the vault to get a table of your
facts grouped by kind. Copy `docs/vault/Факт.md` into your templates
folder for the Templates core plugin. In mirror, a file made from it
does nothing yet (5c).

In mirror, **edits you make in the vault are not applied**. Anchor
records that a file changed, and the next change to that fact in
Anchor overwrites your edit. Your own extra properties survive that
rewrite. A journal day you edit by hand is never written again.

## Turning it off

Set `VAULT_MODE=off` on the bot. The bot then makes no request to the
vault service at all. The vault service can keep running or be
stopped; nothing depends on it.
