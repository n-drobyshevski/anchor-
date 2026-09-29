# Setting up the vault service (milestone 8a)

You do these steps once, by hand. Claude Code never does them: it may
not read the vault or hold its credentials (CLAUDE.md). At the end,
`/vault` in Telegram tells you the sync is running. Until 8b nothing
appears in your vault: 8a only proves the pipe works.

## What you need

- An Obsidian Sync subscription. Standard gives 1 synced vault, 1 GB,
  and one month of version history; Plus gives twelve months. The only
  thing that depends on which one you have is a line in `/delete`'s
  confirmation, which arrives in 8b.
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
| Source → Config file path | leave **empty**: the bot's root `railway.json` must not apply here |
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

If the logs reach `ob sync started` but the deploy fails with
`Healthcheck failed!`, the image predates the fix that makes vaultd
listen on IPv4 as well as IPv6 (docs/decisions.md). Redeploy from the
latest `main`.

## 6. Mirror: your facts in Obsidian (8b)

Set `VAULT_MODE=mirror` on the bot. Within a minute, facts start to
appear in `Anchor/Memory/` and days in `Anchor/Journal/`, 50 files a
minute until everything is there. `/vault` then says `· фактов N`.

Copy `docs/vault/Memory.base` into the vault to get a table of your
facts grouped by kind. Copy `docs/vault/Факт.md` into your templates
folder for the Templates core plugin. In mirror, a file made from it
does nothing yet (8c).

In mirror, **edits you make in the vault are not applied**. Anchor
records that a file changed, and the next change to that fact in
Anchor overwrites your edit. Your own extra properties survive that
rewrite. A journal day you edit by hand is never written again.

## 7. Your notes: personal and knowledge (8e)

Anchor sees a note outside `Anchor/` only if you have given it a class,
and only after you say `/vault notes on`. Nothing is indexed or used in
a conversation yet: that is 8d. 8e lets you classify your notes and
check the classification.

| Class | Meaning | Where it may go |
|---|---|---|
| *(none)* | The default. Invisible to Anchor. | nowhere |
| `never` | Invisible, even inside a folder a rule would include. | nowhere |
| `personal` | About you: your life, health, relationships, plans, feelings. | only the conversation with you |
| `knowledge` | Generic, true whoever reads it: a note on CCRU, on GCP IAM. | the conversation, as reference material |
| `lens` | Knowledge you chose as Echo's frame: people and concepts (L1, section 8). | wherever knowledge goes, except Claude's write tools; with `LENS_ENABLED`, Claude Code through its own role |

Two things set a class:

- **a property on the note:** `anchor: personal`, `anchor: knowledge`
  or `anchor: never`, exactly, at the top level of its properties;
- **a folder rule** in `Anchor/settings.md`. Copy
  `docs/vault/settings.md` into the vault as `Anchor/settings.md` and
  edit the lists (section 8 adds the two lens ones). A rule covers its
  folder and everything below it.

When the two disagree, **the stricter class wins**: `never` beats
`personal`, which beats `knowledge`. A note marked `knowledge` inside a
personal folder is personal, and `/vault` counts it as a conflict. A
note that mixes both kinds is personal: split it if you want the
generic part used as knowledge. Put a private aside inside a knowledge
note in an Obsidian comment (`%% … %%`); 8d strips comments before
indexing.

Notes you marked `anchor: read` in 8a now count as personal. `/vault`
counts them under «проверить» until you change them to `personal` or
`knowledge`. Any other value (`anchor: Knowledge`, a typo) hides the
note and is counted as «неизвестная метка». So is a note whose
properties cannot be read, such as two `anchor:` lines left by a Sync
merge.

**A broken `Anchor/settings.md` hides every note** until it is fixed:
invalid YAML, an unknown or misspelled key, a list that is not a list,
a path with a leading `/` or a `..`, or a missing `anchor: settings`.
Dropping only the broken rules would also drop `never_folders`. A
missing file is fine: it just means there are no folder rules.

Then, in Telegram:

- `/vault notes on`: Anchor may read classified notes. The reply says
  what that means.
- `/vault`: one more line, for example
  `Заметки: личные 12 · знания 40 · проверить: конфликт 2, anchor: read 3 · не прочитано: неизвестная метка 1`.
  It holds counts only: Anchor never learns the names of notes it
  cannot see.
- `/vault notes off`: forget everything read from notes. `/delete` does
  the same and turns notes off.

## 8. The lens: notes Echo reasons with (L1)

`anchor-lens-plan.md`. The **lens** is a set of knowledge notes you
pick as the frame for Echo's self-improvement: people and concepts
(Ashby, Beer, requisite variety…). In L1 it is stored and Claude Code
can read it (docs/claude-access.md → Lens notes); from L2 the weekly
review picks lens notes and grounds its proposals in them while
`LENS_ENABLED` is on (README, "Milestone L2").

**Membership is always yours.** Two things make a note lens:

- the property `anchor: lens` on the note;
- a folder rule in `Anchor/settings.md`: `lens_folders: [Lens]`.

`lens` is the **least strict** class: `never` > `personal` >
`knowledge` > `lens`. So:

- `anchor: knowledge` on a note inside a lens folder keeps that one
  note out of the lens;
- `anchor: lens` inside a personal or never folder is not lens;
- a lens folder **inside** a knowledge, personal or never folder
  makes the whole settings file invalid, and `/vault` says so: the
  stricter rule would otherwise turn every note in it into its own
  class and leave the lens silently empty. Use `Lens`, not
  `Library/Lens` when `Library` is a knowledge folder.

**People and concepts, by folder.** `lens_person_folders: [Lens/People]`
lists folders whose notes are about a person; every other lens note is
a concept. Each entry must be inside (or equal to) a `lens_folders`
entry, or the settings file is invalid and, as with any error there,
every note is hidden until you fix it.

For everything that already reads knowledge, a lens note **is**
knowledge: it is indexed with the knowledge notes, and Claude's
`search_library` finds it. The difference is writing: **Claude's write
tools refuse lens notes**, as source or as destination, so a claude.ai
chat cannot change what Echo reasons with. Only you change the lens.
`list_tree` marks lens notes as lens.

**The bot stores the lens whole only when `LENS_ENABLED` is set**, on
top of `/vault notes on` and `VAULT_KNOWLEDGE_ENABLED`. Then every sync
pass keeps each lens note (title, person or concept, the frontmatter
`summary`, its `aliases` (L3), the text) and the links between knowledge and lens notes.
A link to a note the bot may not see is counted, never named. Turn any
of the three off and the next pass deletes the stored lens.

In Telegram:

- `/lens`: on or off, how many notes (people, concepts), a warning if
  there are more than `LENS_CATALOG_MAX_NOTES` (300), whether Claude
  Code may read it, and how many reads today.
- `/lens code on`, `/lens code off`: open or close Claude Code's access
  (docs/claude-access.md → Lens notes has the one-time setup).

## 9. The lens garden: a weekly report (L3)

Once a week the bot can look for gaps in the lens (missing links,
missing notes, tensions, bridges between groups of notes), send them
to you in one Telegram message with a button row per gap, and write
them to the vault as a report:

```
Anchor/Reports/Lens garden 2026-W40-k3f7qa.md
```

`Anchor/Reports/` is the third folder the bot may write, after
`Memory/` and `Journal/`: only `.md` files directly inside it, never a
subfolder, never `Anchor/Reports.md`.

**Deploy the vault service first.** Redeploy `vault` from this
repository before turning the garden on: an older vault service
refuses writes to `Anchor/Reports/`. The bot copes (it counts the
refusal as `reports_refused=` in its pass log and tries again each
minute, and the rest of the pass is unaffected), but no report appears
until the service is new. Then set `LENS_GARDEN_ENABLED=true` on the
bot, on top of `LENS_ENABLED`, with `VAULT_MODE` `mirror` or `sync`.
`GARDEN_MAX_TOKENS` (2000) caps the model's answer.

The report lists this week's gaps by kind with their status, what is
still open from earlier weeks, and the lens's structure: hubs, named
groups, notes with no links, dead ends, and notes that are linked to
but do not exist. It links only to lens notes; the model's text in it
is escaped, so it can hold no link, tag or embed of its own.

**Edits in the report are not read.** Mark gaps with the Telegram
buttons; the report follows on the next pass. Edit the file and the
bot never touches it again, like a journal day. Delete it and, in
`sync`, it is not written again (in `mirror` it simply stays gone
until the next week's report). Only the latest week's report is kept
up to date; older ones stay as they were.

Turning notes consent, `VAULT_KNOWLEDGE_ENABLED` or `LENS_ENABLED` off
deletes the garden's gaps and stops the writing; reports already in
the vault stay until you delete them, or `/delete` removes them with
the bot's other files in `Anchor/Memory/` and `Anchor/Journal/`. A report from before a `/delete`, re-uploaded by
a device that was offline, has the old epoch in its name and is
deleted, never adopted.

## Turning it off

Set `VAULT_MODE=off` on the bot. The bot then makes no request to the
vault service at all. The vault service can keep running or be
stopped; nothing depends on it.
