# Grok access: opening your data to grok.com, on purpose

Anchor can give the Grok chatbot (grok.com, not the xAI API) read-only
access to your data, but only when you ask for it, only to what you
pick, and only for a limited time. It uses grok.com's custom MCP
connector (Connectors → New Connector → Custom). That connector needs
a public HTTPS URL that speaks MCP Streamable HTTP.

## Turning it on

1. Set `GROK_ACCESS_ENABLED=true` in the Railway service variables and
   redeploy. It needs webhook mode (`MODE=webhook`, `PUBLIC_URL` set),
   because that is the only mode that serves HTTP.
2. With the flag off, the `/mcp/...` route does not exist and `/grok`
   refuses. `/revoke` works either way.

## Using it

1. In Telegram, send `/grok`. You get a picker with everything off:
   - **Память**: active memories.
   - **Журнал**: journal entries and check-ins (rating, result, note).
   - **Диалоги**: your messages and the bot's, plus scene summaries,
     going back 7 / 30 / 90 days (you choose). It never includes
     out-of-character (`/out`) messages, welfare exchanges, or bot
     plumbing.
   - **Состояние**: focus, main action, streak, intensity, and the last
     7 days of spend.

   Choose a lifetime too (1 h / 24 h / 7 d; `GROK_GRANT_MAX_HOURS` caps
   it), then press **Разрешить**.
2. The same message turns into a one-time link, `https://<PUBLIC_URL>/mcp/<token>`.
   Paste it into grok.com → Connectors → New Connector → Custom, then
   delete the Telegram message. The token is not stored anywhere else;
   the database keeps only its sha256.
3. Ask Grok about your data. On the first read, and then at most every
   10 minutes, the bot tells you what Grok read, for example «Grok
   прочитал: память (12)».
4. `/revoke` closes every open grant immediately. Grants also close by
   themselves when they expire, and `/delete` wipes them with
   everything else. After revoking, remove the connector in grok.com
   too.

## What leaves, and what doesn't

- Anything Grok reads goes to xAI and stays in that Grok conversation
  (and in whatever memory Grok keeps). **Revoking stops further reads.
  It cannot recall what was already read.**
- Grok can only read. None of the tools write, and none reach the
  queue, the raw Telegram updates, pending memories, or research data.
- Every refusal returns the same `404: Not Found` as a path that does
  not exist: feature off, bad token, expired, revoked. Each grant is
  rate-limited (`GROK_MAX_CALLS_PER_MINUTE`).
- Logs carry the grant id, the tool name and a row count, never the
  token or any content. aiohttp's access log is disabled because it
  would print the path, and the path contains the token.

## For developers

- Grants: `app/core/grants.py` (this is also where each scope's content
  is decided). Table `access_grant`, migration `5c8e1d2b7a94`.
- Endpoint: `app/web/mcp.py`. It is stateless JSON-RPC:
  `initialize`, `ping`, `tools/list`, `tools/call`.
- Telegram UI: `app/tg/grok.py`. The callback data is
  `g:<action>:<mask>:<period>:<ttl>:<epoch>`.
- Tests: `tests/test_grok_access.py`.
- Claude Code has no access to this: it never sees a token, and
  `docs/claude-access.md` still applies.
