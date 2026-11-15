# Echo — the archiver: a YouTube link becomes a note in the vault (plan)

Version: 2026-11-15 (rev. 2, decisions settled) · Scope: you send Echo a YouTube link; a separate **archiver** service transcribes the video, cleans the transcript lightly, splits it into topics and writes a summary; Echo shows you the result and, on your tap, writes it into the vault as knowledge notes.
Parent docs: `anchor-phase8-plan.md` (vaultd, the write rules), `anchor-phase8e-plan.md` (classes, §7 contract), `anchor-lens-plan.md` §9 and §14.5 (Echo's inbox writer, the pattern this copies), `anchor-phase4-plan.md` (jobs, caps, cards). The archiver's own design is in the `n-drobyshevski/archiver` repository (README). For this feature, **this file wins**. Every other invariant stays in force.

Milestones are **V0–V3** (§12). Nothing is built here yet. §14's decisions are settled (rev. 2); the plan already follows them.

Rev. 2 settles §14 and adds §15: a video note is joined to the notes you already have, by links and tags.

---

## 0. Goal

**Where things stand today:**
- `archiver` exists as a Python library and CLI in its own repository: `yt-dlp` metadata and audio → ElevenLabs Scribe speech-to-text → cleanup with Claude Sonnet → topics and summary with Claude Opus → a Markdown note written into a local vault folder. Its tests pass with the outside services faked. **It has not yet made one real speech-to-text or Claude call** (§12, V0).
- It runs only where the vault folder is on disk, and only where YouTube lets the audio be downloaded. A datacenter address was refused the audio (HTTP 403) while metadata was served.
- Echo has the two things it lacks: a Telegram front end, and `vaultd`, a server-side way into the vault.
- Nothing in Echo touches video, audio or transcription, and no vaultd route can take such a note: the inbox route fixes the frontmatter to `anchor`, `source_urls`, `gap` and caps a note at 32 KB; the 64 KB knowledge route is Claude's writer.

**After this plan:**
- `/watch <ссылка>` in Telegram accepts a YouTube link and answers at once.
- The archiver service does the work in the background. The chat stays responsive throughout.
- When it finishes, Echo sends one card: title, length, number of topics, what it cost, the short summary, and the buttons «в хранилище» and ✖.
- «в хранилище» writes the note into `Echo/Videos` in the vault, as **knowledge**. Obsidian Sync carries it to your devices. `/watch undo` takes the newest one back.
- The note links to the knowledge and lens notes you already have on the people and ideas the video is about, and reuses your tags (§15).

**The principle throughout:** the archiver service receives a link and returns text. It never sees a dialog, a memory, a note or a credential of Echo's, and it cannot reach the database or the vault. Only the bot writes to the vault, only through vaultd, and only on your tap.

## 1. Where this sits

- **After 8e and L4.** It needs vaultd, the classifier, Echo's provenance and changesets, and the `echo_changeset` ledger.
- **A third Railway service**, `archiver`, beside the bot and `vault`. The reasons are in the bot's own design:
  - the worker runs one thing at a time and reclaims a job after 5 minutes (`app/db/queue.py`), so a transcription inside it would freeze the chat and could run twice;
  - the bot's image has no way to add a system package, and the archiver needs a JavaScript runtime for `yt-dlp`.
- **The code stays in its own repository.** Railway builds the `archiver` service from `n-drobyshevski/archiver`. This repository gains a client, a command, a job kind, a writer and a table; it imports nothing from the archiver, and an AST test pins that, as for vaultd.
- **Unchanged:**
  - Every model call the bot itself makes still goes through OpenRouter with `data_collection=deny`. The archiver's calls are its own (§8).
  - `app/research/` and its fetcher are not used and not changed. The archiver is not research: it has no search, no packet and no cards to adopt as memories.
  - Personal notes, dialogs and memory reach nothing new.

## 2. Out of scope (do not build)

- Any site but YouTube. Playlists, channels, live streams.
- Echo reading the video notes back into a prompt. They are ordinary knowledge notes: `search_library` and W2 can reach them, the lens only if you promote one yourself.
- Detecting links in ordinary chat messages. A link in chat stays chat; only `/watch` starts a job.
- The archiver downloading media on the server (§5), proxies, cookies, or any workaround when YouTube or ElevenLabs refuses.
- A second speech-to-text provider; YouTube captions.
- Editing or re-running a note that is already in the vault. A second `/watch` of the same video makes a second note.
- Writing a link into any note you already have, and links to notes that do not exist (§15).

---

## 3. The flow

1. **`/watch <ссылка>`.** The bot checks, in order: `ARCHIVER_ENABLED`; the vault is in `mirror` or `sync`; the link's host is `youtube.com`, `www.`/`m.youtube.com` or `youtu.be` and it carries an 11-character video id; today's quota (`ARCHIVER_VIDEOS_PER_DAY`); the archiver's daily cap (§9). It inserts a `video_job` row, asks the archiver to start (`POST /v1/jobs`), enqueues a `VIDEO_POLL` job and replies «принято».
2. **The archiver works.** Metadata, transcription, cleanup, outline, rendering (§4, §5). It refuses a live stream, a video over `ARCHIVER_MAX_MINUTES`, and a job whose estimated cost is over `ARCHIVER_JOB_USD_CAP`, before any paid call.
3. **`VIDEO_POLL`.** Every 30 seconds the bot's worker asks the archiver for the job's status (one short request) and defers itself (`defer_job`, no attempt spent) until the status is `done` or `failed`, or 3 hours have passed (then `failed`, reason `stale`).
4. **The card.** On `done` the bot records the cost and sends the card, under the usual quiet-hours and pause holds (`may_report_now`). On `failed` it sends one line with a reason from a fixed list (§6).
5. **«в хранилище».** The tap fetches the rendered note from the archiver, screens it (`note_checks.check_content`: the instruction list and the secret redactor), and writes it through vaultd (§7). ✖, or 14 days without a tap, deletes the result on the archiver.
6. **`/watch undo`** removes the newest video note Echo wrote, through vaultd's undo, within 14 days.

## 4. The archiver service

Built in the `archiver` repository as `archiver serve`: the existing pipeline behind a small HTTP API.

- **Private only.** It listens on Railway's private network. Like vaultd, it refuses to boot with a public domain or TCP proxy.
- **Auth.** Every route but `/healthz` needs `Authorization: Bearer <ARCHIVER_API_TOKEN>`.
- **Routes:**

  | Method and route | Body | Answer |
  |---|---|---|
  | `GET /healthz` | – | `{ok}` |
  | `POST /v1/jobs` | `{url}` | `{id}`; 400 unless the URL is a YouTube video link |
  | `GET /v1/jobs/{id}` | – | `{status, stage, error, meta, parts, usage, cost_usd}`; `meta` is title, channel, length and language; `error` is a code from §6 |
  | `GET /v1/jobs/{id}/note` | – | `{files: [{name, content}]}`, only when `done` |
  | `DELETE /v1/jobs/{id}` | – | removes the job and everything cached for it |
  | `POST /v1/purge` | – | removes every job (for `/delete`) |

- **State.** A volume holds the per-video cache (the archiver already resumes from it, so a restart mid-job costs nothing twice) and the job list. No database.
- **One job at a time**, the rest queued. A restart resumes the queue.
- **Its secrets** (`ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`) exist only on this service. The bot never holds them.
- **Logs** carry job id, stage, status, counts, seconds and cost. Never a URL, a video id, a title or text.

## 5. Getting the audio, and the one unverified step

- **Metadata** (title, channel, date, length, chapters, description) comes from `yt-dlp` with `download=False`. From a datacenter address this is unreliable: in testing it worked for two videos and was refused with YouTube's bot check for a third, the same day. V0 has to measure it from Railway.
- **Audio is not downloaded by the service.** YouTube refused it from a datacenter address, and this plan allows no proxy or cookie workaround (§2). Instead the service passes the video URL to ElevenLabs, whose speech-to-text API accepts a `source_url` and fetches the media itself.
- **This is unverified.** Whether `source_url` works reliably with YouTube links has not been tested. V0 (§12) tests it before anything else is built. If it fails, this plan stops there: the archiver stays a laptop tool (§14.6).
- `yt-dlp` does not read `robots.txt` and presents itself as a browser client. That differs from `app/research/fetch.py`, which does neither. It is confined to metadata, lives outside `app/research/`, and is named here so the difference is a decision, not an accident (§14.4).

## 6. Bot: storage, command, job

- **`video_job`**: `id`, `created_at`, `status` (`queued`, `running`, `done`, `failed`, `adopted`, `declined`, `expired`), `archiver_ref`, `video_id`, `title`, `duration_s`, `parts`, `cost_usd`, `error`, `message_id`. CHECK constraints on `status` and `error`. `title` and `video_id` are your data: they are in `/export`, removed by `/delete`, and absent from the debug view.
- **`debug.video_job`** (own migration): `id`, `created_at`, `status`, `duration_s`, `parts`, `cost_usd`, `error`.
- **`error`** is one of: `not_video`, `live`, `too_long`, `over_cap`, `blocked` (YouTube refused), `no_speech`, `stt_failed`, `llm_failed`, `unavailable` (the archiver did not answer), `stale`. Each has one Russian line; none carries text from the video.
- **`app/archiver/client.py`**: `ArchiverClient`, built like `VaultClient`: bearer header, no redirects, a timeout, `ARCHIVER_URL` must be `*.railway.internal` or localhost.
- **`/watch`** in `BOT_COMMANDS` and `build_router`, with `_once`/`_reply_once`, and a ✏️ button under «Знания» in `/menu`.
- **`VIDEO_POLL`** in `_run_job`: one status request, then complete or defer. It makes no model call and writes nothing to the vault. The card is sent after `complete_job`, like `_send_research_done`.
- **`/delete`** cancels every open `video_job`, calls the archiver's `/v1/purge`, and deletes the rows. Notes already in the vault follow the vault's own rules (8b's purge).
- **Settings** (`app/config.py`, `.env.example`, bounds in `check_runtime_settings`): `ARCHIVER_ENABLED` (false), `ARCHIVER_URL`, `ARCHIVER_API_TOKEN`, `ARCHIVER_VIDEOS_PER_DAY` (3, 1–10), `ARCHIVER_MAX_MINUTES` (180, 5–240), `ARCHIVER_JOB_USD_CAP` (2.00), `ARCHIVER_DAILY_USD_CAP` (3.00).

## 7. vaultd changes, and the writer

A third Echo writer purpose, after the inbox and the streams. vaultd is deployed before the bot, as in L4.

- **`echo_videos`** in `Anchor/settings.md`: the folder for video notes, default `Echo/Videos`, resolved like `echo_inbox`. Everything in it is knowledge.
- **`PUT /v1/echo/videos`**, body `{files: [{name, content}], changeset}`. Every check is made for every file, inside the store's lock, before a byte is written; one refusal refuses the whole changeset.
  - `name` is a bare `.md` basename; vaultd builds the path. Create-only, with the inbox's `name 2`…`name 9` rule.
  - Frontmatter keys are exactly `anchor`, `title`, `url`, `video_id`, `channel`, `published`, `duration_min`, `language`, `tags`, `created`, `part`, `parts`. `anchor` is `knowledge`, never `lens`. `url` is `https://www.youtube.com/watch?v=<video_id>` and nothing else. Types are checked; `tags` is a list of at most 7 short strings.
  - Each file is at most `ECHO_VIDEO_WRITE_MAX_BYTES` (48 KB), well under `NOTE_MAX_BYTES`, so every note stays readable. A changeset of several files is larger than `BODY_MAX_BYTES` (64 KB), so this route alone reads a body of up to `ECHO_VIDEO_BODY_MAX_BYTES` (640 KB).
  - Caps, as code constants: 12 files per changeset, 5 changesets a day.
  - Stamped `anchor_edited_by: echo`, recorded as an `echo` changeset with its own purpose, undone only by Echo.
- **Long videos are several notes.** An hour of Russian speech is roughly 100 KB of text. The archiver renders an **index note** (frontmatter, the embed, TL;DR, key points, contents) and **part notes** of at most 48 KB each, cut at topic boundaries, linked from the index with `[[…]]`. A short video is one note. This needs a split mode in the archiver's renderer (V1).
- **`app/core/echo_write.py`** gains `adopt_video`, the one caller of `put_echo_videos`, run only by the «в хранилище» tap. `tests/test_echo_isolation.py` is extended to pin both. It follows `adopt_research`: the `echo_changeset` row is committed first, a lost answer is replayed with the same reference, a 403 deletes the row.

## 8. Third parties and privacy

Two new direct processors, both called only by the archiver service:

| Who | Receives | Never receives |
|---|---|---|
| ElevenLabs | the video's URL, and through it the audio | anything of yours |
| Anthropic (API) | the transcript, and the video's title, channel, description and chapter titles | anything of yours |
| YouTube | a metadata request from the service's address | – |

- What leaves is public material. What it reveals about you is **which videos you archive**. That is stated in `/privacy`.
- Structurally, not by filter: the bot sends the archiver one URL and reads back one job. No dialog, memory, note, summary or state is an input to any archiver call.
- The retention and training terms of both vendors' APIs are to be read and summarised in `docs/privacy.md` in V3, not assumed here.
- **To update in V3:** `PRIVACY_TEXT` and `docs/privacy.md` (same line count, `tests/test_privacy.py`), README (Status row, Commands row, Privacy, a milestone section), `docs/decisions.md`, `docs/secrets.md` (three new secrets and their rotation), `docs/vault-setup.md` (`echo_videos`), `.env.example`, the guard hook's secret list, `/export` and `/delete` coverage.

## 9. Cost and caps

- Expected, for one hour of video: about $0.20 for speech-to-text plus $0.30–0.60 for Claude. Not yet measured (V0).
- The archiver reports its Claude cost per job; the speech-to-text cost is computed from the length at a stored price. Both go into `spend_ledger` as one row, category `archiver`.
- **Its own caps, outside `DAILY_USD_CAP`** (§14.3): a single video costs about what the bot's whole day does, and it is spent with other vendors. `ARCHIVER_JOB_USD_CAP` is checked from the video's length before any paid call; `ARCHIVER_DAILY_USD_CAP` at `/watch`. `/state` shows the day's archiver spend on its own line.
- The quota counts accepted jobs, including failed ones.

## 10. What each class may reach (8e §7, extended)

| Consumer | Personal | Knowledge | Lens |
|---|---|---|---|
| The archiver service (any call it makes) | never | never | never |
| Echo's video writes | – | new notes in `echo_videos` only, create-only, 14-day undo | never |

A video note is knowledge from the moment it is written. It reaches what any knowledge note reaches, and nothing else.

## 11. Tests

- **Isolation (AST):** no import between `app` and the archiver; `put_echo_videos` named only by `echo_write.py`, `adopt_video` only by the Telegram module whose tap runs it; `VIDEO_POLL` names no writer and no model call.
- **vaultd:** every refusal of §7, the caps, the multi-file changeset written whole or not at all, replay, undo, the class of the result.
- **Bot:** the order of checks in `/watch`, quota and caps, the poll's defer and stale paths, the card held by quiet hours, adopt and its replay, `/delete` during a running job, export coverage, the log allowlist.
- **Archiver:** its own suite, plus the HTTP API, the split renderer and the `source_url` path with a stub client.
- **Smoke** (`scripts/smoke.py`, manual, real money): one short video end to end.

## 12. Milestones

- **V0: prove it, in the archiver repository only.** No change here.
  - A real run on a laptop on one Russian and one English video: read the notes, tune the cleanup prompt and the fidelity thresholds. (The first Russian run is done: 45 minutes, no chunk fell back to raw text, $0.32 of Claude. The note has not been reviewed yet.)
  - From Railway: does metadata load reliably (it is sometimes refused from datacenter addresses, §5), and does ElevenLabs transcribe a YouTube `source_url`?
  - Measure the real cost per hour.
  - **Gate:** if `source_url` does not work, or metadata is refused too often to rely on, stop (§14.6).
- **V1: the service.** `archiver serve`, the job list, the `source_url` path, the split renderer, the concepts of §15 in the job's answer, the Dockerfile, the boot refusals. Deployed, reachable only from the bot's network, nothing calling it yet.
- **V2: vaultd.** `echo_videos`, `PUT /v1/echo/videos`, caps, the undo purpose. Deployed before V3.
- **V3: the bot.** Settings, the migration and debug view, the client, `/watch`, `VIDEO_POLL`, the card, `adopt_video` with §15's resolver, `/watch undo`, export and delete, spend, the documents of §8, the tests of §11. Off behind `ARCHIVER_ENABLED`.
- **Deferred, not planned:** links recognised in chat; YouTube captions as a cheaper source; a video note offered to the lens garden.

## 13. Threats

- **A video that speaks instructions.** A transcript is untrusted text that ends up where Claude's connector can read it. It lands only as knowledge, never lens; `note_checks.check_content` screens it before the write; only you promote a note. The same gate as L4's inbox.
- **Hostile metadata.** A title or description shaped to break frontmatter, forge a property or plant a link. The archiver writes frontmatter with a YAML dumper and escapes headings; vaultd accepts only the fixed keys with checked types and builds the path itself.
- **Runaway spend.** The daily quota, the length cap, the per-job cap checked before the first paid call, the daily archiver cap; the archiver's cache means a retry never pays twice. A card or a note can never start another job.
- **The archiver service compromised.** It holds two vendor keys and public transcripts, and nothing else: no database URL, no vault token, no bot token. It cannot write to the vault.
- **A request aimed elsewhere.** The bot accepts only YouTube hosts and the archiver checks again; the service calls YouTube, ElevenLabs and Anthropic and nothing else.
- **What you watch, in a log.** Neither service logs a URL, a video id or a title; a test pins the bot's side and the archiver's.
- **A second background writer by the back door.** The poll job cannot write to the vault; the isolation test names it.

## 14. Decisions (settled, rev. 2)

1. **The tap stays.** Nothing is written to the vault without «в хранилище». You see the cost and the summary first, and a bad transcript never reaches the vault. `/watch` alone is not consent to write.
2. **The class is `knowledge`.** Video notes are searchable from claude.ai and editable by W2, like any knowledge note. Never `lens` unless you promote one.
3. **The archiver has its own caps**, outside `DAILY_USD_CAP` (§9).
4. **`yt-dlp`, for metadata only.** Not oEmbed: without the length there is no cap before the paid call.
5. **The command is `/watch`.**
6. **If V0's gate fails, the plan stops** and the archiver stays a laptop tool. Running the service from a home machine would need its own plan.
7. **Direct Anthropic calls.** The archiver calls the Anthropic API directly, as built, not through OpenRouter. It is the first direct Anthropic processor in the system (§8).

## 15. Links and tags: joining the notes you already have (added in rev. 2)

A video note that links to nothing is an island. This section joins it to the knowledge and lens notes on the same people and ideas, without any note of yours leaving the system.

**The rule: the model proposes blind, the code resolves.**

- **The archiver names concepts, and never sees the vault.** Its outline call returns up to 15 `concepts`: the people, organisations, works, theories and terms the video is substantially about. Each has up to five `names` (dictionary form; Russian and English; short and long) and one `relation` line saying how the video bears on it. The call's input is the transcript and the video's metadata, as before. No title, tag or alias from the vault is an input to any archiver call.
- **The bot resolves them, in code, at the tap.** `adopt_video` reads vaultd's `GET /v1/knowledge/graph`, whose nodes are knowledge and lens notes only, each with its title, aliases and tags. A concept matches a note when one of its names equals the note's title or an alias, after normalising (case, `ё`/`е`, punctuation, spaces). No model takes part in the match.
- **What the match adds to the index note:**
  - a section «Связанные заметки»: one line per matched note, `[[Title]]` and the concept's `relation`; at most 12;
  - tags: an existing tag is added when a concept's name equals it. The model's own tags (at most 5) are kept only when the vault already uses them, so a video cannot grow the tag list.
- **What it never does:**
  - link to a note that does not exist. An unresolved `[[name]]` would show up in the garden as a missing note;
  - name, count or hint at a personal, `never` or unclassified note. They are not in the graph route's answer, so they are structurally absent;
  - write into an existing note. Obsidian shows the backlink on the other side by itself;
  - change a note's class, or put a video note into the lens.
- **The same resolver runs in the laptop tool**, against the vault folder on disk, so both paths produce the same links. There it skips `Anchor/` and dot-folders, and reads only titles and frontmatter.
- **Threats.** A link's target is always one of your own titles, so a video cannot forge a link to a path of its choosing. The `relation` line is model text written from the transcript: it is screened with the rest of the note (`note_checks.check_content`) and escaped as inert text. A concept name that matches too much (a note called «Идея») is bounded by the cap of 12 and by names shorter than 3 characters never matching.
- **Tests.** The resolver with each class pair (a personal note of the same title is never linked); normalisation; the cap; the tag rule; an AST test that the archiver client's request carries nothing but the URL.
- **Later, only if the links prove too sparse:** letting the outline call see the titles of knowledge and lens notes, so it can match by meaning («закон Эшби» to a note called «Requisite variety»). That sends vault titles to a third party and would be a new row in §10; it is not part of this plan.
