'use strict';
// Anchor web chat.
//
// Single-page client for the web front-end described in the HTTP API
// contract: GET /api/me picks between the login and chat screens, auth
// is a passphrase then a Telegram one-time code, outbound delivery is
// SSE (/api/events) and inbound is a plain POST (/api/send, /api/press).
//
// Security contract this file must uphold (enforced by a grep test):
//   - no innerHTML/outerHTML/insertAdjacentHTML/document.write/eval/
//     new Function/string setTimeout anywhere below.
//   - every piece of bot or user text is rendered with textContent only
//     (CSS white-space: pre-wrap does the line-wrapping); there is no
//     markdown rendering and no auto-linking, by design -- model output
//     is untrusted.

// ---------------------------------------------------------------------
// DOM references (the page ships all of these; app.js only toggles
// hidden/value/class, it never builds new top-level structure from
// scratch).
// ---------------------------------------------------------------------

const loginEl = document.getElementById('login');
const passphraseForm = document.getElementById('login-passphrase');
const passphraseInput = document.getElementById('passphrase-input');
const passphraseError = document.getElementById('passphrase-error');
const codeForm = document.getElementById('login-code');
const codeInput = document.getElementById('code-input');
const codeError = document.getElementById('code-error');
const codeBackButton = document.getElementById('code-back');

const chatEl = document.getElementById('chat');
const connDot = document.getElementById('conn-dot');
const pauseButton = document.getElementById('pause-button');
const logoutButton = document.getElementById('logout-button');
const reconnectBanner = document.getElementById('reconnect-banner');
const logEl = document.getElementById('log');
const topSentinel = document.getElementById('top-sentinel');
const emptyStateEl = document.getElementById('empty-state');
const typingIndicator = document.getElementById('typing-indicator');
const jumpDown = document.getElementById('jump-down');
const toastRegion = document.getElementById('toast-region');
const composerForm = document.getElementById('composer');
const composerInput = document.getElementById('composer-input');

// ---------------------------------------------------------------------
// Small fetch helpers. Every mutating call sends Content-Type: application/
// json and credentials: 'same-origin' (cookies), per the contract. A 401
// from an already-authenticated area of the app means the session died;
// the two auth endpoints handle their own 401 (bad passphrase/code)
// locally instead, so they don't go through goToLogin().
// ---------------------------------------------------------------------

async function apiCall(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    return { status: 0, ok: false, data: null };
  }
  let data = null;
  try {
    data = await res.json(); // 202/204 responses simply have no body
  } catch {
    // ignore
  }
  return { status: res.status, ok: res.ok, data };
}

function apiGet(path) {
  return apiCall(path, { credentials: 'same-origin' });
}

function apiPost(path, body) {
  return apiCall(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------
// Screen switching.
// ---------------------------------------------------------------------

function showLogin() {
  chatEl.hidden = true;
  loginEl.hidden = false;
}

function showChat() {
  loginEl.hidden = true;
  chatEl.hidden = false;
}

function setLoginStage(stage) {
  // Toggles which login step is visible; also used by "Назад" to reset.
  if (stage === 'code') {
    passphraseForm.hidden = true;
    codeForm.hidden = false;
    hideError(codeError);
    codeInput.value = '';
    codeInput.focus();
  } else {
    codeForm.hidden = true;
    passphraseForm.hidden = false;
    hideError(passphraseError);
    passphraseInput.value = '';
    passphraseInput.focus();
  }
}

function showError(el, text) {
  el.textContent = text;
  el.hidden = false;
}

function hideError(el) {
  el.hidden = true;
  el.textContent = '';
}

function minutesText(retryAfterSeconds) {
  const m = Math.max(1, Math.ceil((retryAfterSeconds || 60) / 60));
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

// A forced return to the login screen: session expired, logout, or the
// SSE stream noticed we're no longer authenticated. Always restarts at
// the passphrase step, mirroring what "Назад" does.
function goToLogin() {
  closeSSE();
  setLoginStage('none');
  showLogin();
}

// ---------------------------------------------------------------------
// Login flow.
// ---------------------------------------------------------------------

async function onPassphraseSubmit(e) {
  e.preventDefault();
  hideError(passphraseError);
  const value = passphraseInput.value;
  if (!value) return;
  setFormBusy(passphraseForm, true);
  const res = await apiPost('/api/auth/passphrase', { passphrase: value });
  setFormBusy(passphraseForm, false);
  if (res.status === 200) {
    setLoginStage('code');
  } else if (res.status === 401) {
    showError(passphraseError, 'Неверный пароль или код.');
  } else if (res.status === 429) {
    showError(passphraseError, minutesText(res.data && res.data.retry_after));
  } else {
    showError(passphraseError, 'Что-то пошло не так. Попробуй ещё раз.');
  }
}

async function onCodeSubmit(e) {
  e.preventDefault();
  hideError(codeError);
  const value = codeInput.value;
  if (!value) return;
  setFormBusy(codeForm, true);
  const res = await apiPost('/api/auth/code', { code: value });
  setFormBusy(codeForm, false);
  if (res.status === 200) {
    await enterChat();
  } else if (res.status === 401) {
    showError(codeError, 'Неверный пароль или код.');
  } else if (res.status === 429) {
    showError(codeError, minutesText(res.data && res.data.retry_after));
  } else {
    showError(codeError, 'Что-то пошло не так. Попробуй ещё раз.');
  }
}

function setFormBusy(form, busy) {
  const btn = form.querySelector('button[type="submit"]');
  if (btn) btn.disabled = busy;
}

async function logout() {
  await apiPost('/api/auth/logout', {});
  goToLogin();
}

// ---------------------------------------------------------------------
// Connection indicator + SSE.
// ---------------------------------------------------------------------

const CONN_LABELS = { ok: 'на связи', reconnecting: 'переподключение', down: 'нет связи' };

function setConnState(state) {
  connDot.dataset.state = state;
  connDot.setAttribute('aria-label', CONN_LABELS[state]);
}

let es = null;
let sseErrorCount = 0;
let reconnectTimer = null;
let typingTimer = null;

// Buffers 'message'/'edit' events that arrive before the initial
// GET /api/history page has finished loading. enterChat() opens the
// SSE stream and starts that fetch at roughly the same time, so a reply
// that finishes in that window used to either race ahead of (and so
// render above) the history page it belongs after, or -- since the
// live event's id (the sink's negative id) and the same row's id once
// it shows up in history (a positive DB id) are different id spaces --
// render twice under two different ids. Buffering and replaying after
// the initial page lands does not fix the id-space mismatch (a known,
// accepted limitation shared with the sink/hub design), but it does
// fix the far more common case: ordering, so history is never inserted
// above a live row that already rendered.
let historyReady = false;
let pendingLive = [];

function teardownEventSource() {
  if (es) {
    es.removeEventListener('error', onSSEError);
    es.close();
    es = null;
  }
}

function connectSSE() {
  teardownEventSource();
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  es = new EventSource('/api/events');
  es.addEventListener('open', () => {
    sseErrorCount = 0;
    setConnState('ok');
    reconnectBanner.hidden = true;
  });
  es.addEventListener('error', onSSEError);
  es.addEventListener('message', (ev) => {
    hideTyping();
    const msg = JSON.parse(ev.data);
    if (!historyReady) {
      pendingLive.push({ kind: 'message', msg });
      return;
    }
    renderIncoming(msg);
  });
  es.addEventListener('edit', (ev) => {
    const edit = JSON.parse(ev.data);
    if (!historyReady) {
      pendingLive.push({ kind: 'edit', edit });
      return;
    }
    applyEdit(edit);
  });
  es.addEventListener('typing', showTyping);
  es.addEventListener('toast', (ev) => showToast(JSON.parse(ev.data).text));
}

function flushPendingLive() {
  const buffered = pendingLive;
  pendingLive = [];
  for (const item of buffered) {
    if (item.kind === 'message') renderIncoming(item.msg);
    else applyEdit(item.edit);
  }
}

// EventSource's own automatic reconnect only fires while the browser
// considers the connection recoverable; a non-200 response (a 429 from
// the 3-stream cap, or a 502/503 from a proxy mid-deploy) instead moves
// it straight to CLOSED, permanently, with no further retry of its own.
// This is what restarts it in that case, with capped exponential
// backoff -- sseErrorCount is deliberately *not* reset by a scheduled
// reconnect (only a successful 'open', or an explicit closeSSE(), resets
// it), so the backoff and the reconnect banner both keep accounting for
// the whole outage rather than restarting their count on every attempt.
function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = Math.min(30000, 1000 * 2 ** Math.min(sseErrorCount, 5));
  reconnectTimer = setTimeout(connectSSE, delay);
}

async function onSSEError() {
  sseErrorCount += 1;
  setConnState('reconnecting');
  if (sseErrorCount >= 3) reconnectBanner.hidden = false;
  if (es && es.readyState === EventSource.CLOSED) {
    scheduleReconnect();
  }
  // Only a server-confirmed "you are not logged in" sends the app back
  // to the login screen. A network failure (status 0) or a 5xx/502
  // during a deploy both make /api/me's own fetch fail or come back
  // non-200 too -- treating *that* as "log out" used to throw away a
  // perfectly good session on the very first blip, before the
  // reconnect banner above even had a chance to show.
  const res = await apiGet('/api/me');
  if (res.status === 401 || (res.status === 200 && res.data && res.data.authenticated === false)) {
    goToLogin();
  }
}

function closeSSE() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  teardownEventSource();
  sseErrorCount = 0;
  reconnectBanner.hidden = true;
  setConnState('down');
}

function showTyping() {
  typingIndicator.hidden = false;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(hideTyping, 6000);
}

function hideTyping() {
  typingIndicator.hidden = true;
  clearTimeout(typingTimer);
}

// ---------------------------------------------------------------------
// Toasts (aria-live=polite status region; auto-dismiss).
// ---------------------------------------------------------------------

function showToast(text) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text;
  toastRegion.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ---------------------------------------------------------------------
// Message log: rendering, dedupe, keyboards, scrolling.
// ---------------------------------------------------------------------

// id (DB row id, or the sink's negative id) -> {row, bubble}. Backs both
// dedupe (an id already here is skipped) and `edit` events (looked up by
// id). POST /api/send *does* mirror the sender's own message back over
// every live SSE stream, including the sending tab's own (so a second
// open tab/device shows it too) -- see `pendingOwn` below for how that
// echo gets reconciled with the optimistic bubble instead of rendered
// as a second, disconnected copy of it.
const rendered = new Map();

// FIFO queue of `{text, row, bubble}` for this tab's own outgoing
// messages that have not yet been matched to a server id. Registered
// synchronously in `sendMessage()`, before the POST is even sent, so
// it is in place no matter which of two independent round trips
// reaches this tab first:
//   1. POST /api/send's own 202 response, carrying `update_id`.
//   2. The SSE `message` event that the same request's handler
//      publishes to every live stream (including this one) -- and
//      publishes *before* that response is written, since it happens
//      earlier in the same handler. That ordering means the SSE echo
//      routinely wins the race, not just occasionally: registering
//      the id only in the 202 handler (the original approach) left
//      `renderIncoming` with no entry in `rendered` yet whenever the
//      echo arrived first, so it built a second bubble for the exact
//      same message -- a real, easily reproduced duplicate, not a
//      theoretical one.
// Whichever side wins claims the pending entry (by text, oldest
// match first) and reconciles it with the winning id instead of
// building a new row; the loser then finds nothing left to do.
const pendingOwn = [];

function claimPendingOwn(text) {
  const i = pendingOwn.findIndex((p) => p.text === text);
  return i === -1 ? null : pendingOwn.splice(i, 1)[0];
}

function removePendingOwn(entry) {
  const i = pendingOwn.indexOf(entry);
  if (i !== -1) pendingOwn.splice(i, 1);
}

function formatTime(ts) {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return { short: `${hh}:${mm}`, full: d.toLocaleString('ru-RU') };
}

function buildKeyboard(messageId, rows) {
  const wrap = document.createElement('div');
  wrap.className = 'keyboard';
  for (const row of rows) {
    const rowEl = document.createElement('div');
    rowEl.className = 'keyboard-row';
    for (const btn of row) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'keyboard-button';
      b.textContent = btn.text;
      b.addEventListener('click', () => pressButton(messageId, btn.data, wrap));
      rowEl.appendChild(b);
    }
    wrap.appendChild(rowEl);
  }
  return wrap;
}

async function pressButton(messageId, data, keyboardEl) {
  for (const b of keyboardEl.querySelectorAll('button')) b.disabled = true;
  const res = await apiPost('/api/press', { message_id: messageId, data });
  if (res.status === 202) return; // stays disabled until an `edit` (or reload) replaces it
  if (res.status === 401) {
    goToLogin();
    return;
  }
  if (res.status === 409) {
    // Stale: the allowlist for this message moved on (an edit replaced
    // it, or /weblogout cleared it). Re-enabling let the user keep
    // pressing buttons the server would only ever reject again with
    // another 409, one toast at a time. Removing the keyboard instead
    // makes the one rejection the last one.
    showToast('устарело');
    keyboardEl.remove();
    return;
  }
  // Only a network/5xx/429 failure -- something that might succeed on a
  // retry -- re-enables the buttons.
  for (const b of keyboardEl.querySelectorAll('button')) b.disabled = false;
  if (res.status === 429) showToast(minutesText(res.data && res.data.retry_after));
  else showToast('Не отправлено.');
}

function buildMessageRow(msg) {
  const row = document.createElement('div');
  row.className = `msg-row role-${msg.role}`;
  if (msg.kind) row.dataset.kind = msg.kind;

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = msg.text;
  row.appendChild(bubble);

  if (msg.keyboard) row.appendChild(buildKeyboard(msg.id, msg.keyboard));

  const time = document.createElement('div');
  time.className = 'msg-time';
  const t = formatTime(msg.ts);
  time.textContent = t.short;
  time.title = t.full;
  row.appendChild(time);

  rendered.set(msg.id, { row, bubble });
  return row;
}

function applyEdit(edit) {
  const entry = rendered.get(edit.id);
  if (!entry) return;
  if ('text' in edit && edit.text != null) entry.bubble.textContent = edit.text;
  if ('keyboard' in edit) {
    const old = entry.row.querySelector('.keyboard');
    if (old) old.remove();
    if (edit.keyboard) entry.row.appendChild(buildKeyboard(edit.id, edit.keyboard));
  }
}

function updateEmptyState() {
  emptyStateEl.hidden = logEl.querySelector('.msg-row') !== null;
}

function isNearBottom() {
  return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;
}

function scrollToBottom() {
  logEl.scrollTop = logEl.scrollHeight;
  jumpDown.hidden = true;
}

// A live message arriving over SSE: append at the bottom, auto-scroll
// only if the reader was already near the bottom, otherwise surface the
// "new messages" pill instead of yanking their scroll position.
function renderIncoming(msg) {
  if (rendered.has(msg.id)) return;
  if (msg.role === 'user') {
    // The server's echo of a message this tab itself just sent (see
    // `pendingOwn` above) -- adopt the existing optimistic bubble
    // under this id instead of building a disconnected second one.
    // A message a *different* tab/device sent has no matching pending
    // entry and falls through to the ordinary render below.
    const own = claimPendingOwn(msg.text);
    if (own) {
      rendered.set(msg.id, { row: own.row, bubble: own.bubble });
      return;
    }
  }
  const wasNear = isNearBottom();
  const row = buildMessageRow(msg);
  logEl.insertBefore(row, emptyStateEl);
  updateEmptyState();
  if (wasNear) scrollToBottom();
  else jumpDown.hidden = false;
}

// A page of /api/history results, already oldest-first. `initial` is the
// very first page (append at the bottom, then scroll down); every later
// page is older messages loaded by scrolling up (prepend, preserving
// the reader's scroll position).
function insertHistoryPage(messages, initial) {
  const frag = document.createDocumentFragment();
  for (const m of messages) {
    if (rendered.has(m.id)) continue;
    frag.appendChild(buildMessageRow(m));
  }
  if (!frag.childNodes.length) return;
  if (initial) {
    logEl.insertBefore(frag, emptyStateEl);
    updateEmptyState();
    scrollToBottom();
  } else {
    const prevHeight = logEl.scrollHeight;
    const prevTop = logEl.scrollTop;
    const anchor = logEl.querySelector('.msg-row') || emptyStateEl;
    logEl.insertBefore(frag, anchor);
    updateEmptyState();
    logEl.scrollTop = prevTop + (logEl.scrollHeight - prevHeight);
  }
}

let oldestId = null;
let hasMoreHistory = true;
let historyLoading = false;

async function loadHistory() {
  if (historyLoading || !hasMoreHistory) return;
  historyLoading = true;
  // `#log` is `aria-live="polite"`, and a history page can insert up to
  // 50 rows at once (the initial load, or every scroll-to-top refetch).
  // Without this, a screen reader announced every one of them, one at a
  // time, burying whatever a genuinely new live message said next.
  // `aria-busy` suppresses live-region announcements for the region
  // while it is set, so only renderIncoming's and the composer's own
  // rows -- never a history page -- get read aloud.
  logEl.setAttribute('aria-busy', 'true');
  try {
    const initial = oldestId === null;
    const qs = initial ? 'limit=50' : `before=${oldestId}&limit=50`;
    const res = await apiGet(`/api/history?${qs}`);
    if (res.status === 401) {
      goToLogin();
      return;
    }
    if (!res.ok || !res.data) return;
    const { messages, has_more } = res.data;
    hasMoreHistory = !!has_more;
    if (messages.length) {
      oldestId = messages[0].id;
      insertHistoryPage(messages, initial);
    } else if (initial) {
      oldestId = -1; // no history at all; stop the sentinel from refetching forever
      updateEmptyState();
    }
  } finally {
    historyLoading = false;
    logEl.setAttribute('aria-busy', 'false');
  }
}

const historyObserver = new IntersectionObserver(
  (entries) => {
    if (entries.some((e) => e.isIntersecting)) loadHistory();
  },
  { root: logEl, threshold: 0 },
);
historyObserver.observe(topSentinel);

logEl.addEventListener('scroll', () => {
  if (isNearBottom()) jumpDown.hidden = true;
});

jumpDown.addEventListener('click', scrollToBottom);

// ---------------------------------------------------------------------
// Composer: send our own text, with an optimistic bubble and a retry
// path that reuses the same client_key so a retry can never duplicate.
// ---------------------------------------------------------------------

function buildOutgoingRow(text) {
  const row = document.createElement('div');
  row.className = 'msg-row role-user';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  row.appendChild(bubble);
  const status = document.createElement('div');
  status.className = 'msg-status';
  row.appendChild(status);
  return {
    el: row,
    bubble,
    setStatus(text) {
      status.className = 'msg-status';
      status.textContent = text;
    },
    setNote(text) {
      status.className = 'msg-note';
      status.textContent = text;
    },
    setRetry(onRetry) {
      status.textContent = '';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'msg-retry';
      btn.textContent = 'Не отправлено — повторить';
      btn.addEventListener('click', () => {
        btn.remove();
        onRetry();
      });
      status.className = 'msg-note';
      status.appendChild(btn);
    },
  };
}

async function postSend(clientKey, text, row, pending) {
  row.setStatus('отправка…');
  const res = await apiPost('/api/send', { text, client_key: clientKey });
  if (res.status === 401) {
    removePendingOwn(pending);
    goToLogin();
    return;
  }
  if (res.status === 202) {
    row.setStatus('');
    // The server also mirrors this message to every live SSE stream
    // (including this tab's own), so a second open tab/device can show
    // it -- but that means *this* tab's own EventSource may already
    // have received (renderIncoming, via `pendingOwn`) or may still be
    // about to receive an echo of the very message it just
    // optimistically rendered. Registering it under the id the server
    // used (the update_id the 202 body returns) covers the case where
    // this response won the race: `rendered.has(id)` is now true, so
    // if the echo arrives after this, renderIncoming's ordinary dedupe
    // skips it. `removePendingOwn` covers the other case -- the echo
    // already won and reconciled `pending` itself -- by clearing the
    // now-stale queue entry so it cannot wrongly match some later,
    // unrelated message with the same text.
    if (res.data && typeof res.data.update_id === 'number') {
      rendered.set(res.data.update_id, { row: row.el, bubble: row.bubble });
    }
    removePendingOwn(pending);
    return;
  }
  if (res.status === 422) {
    // Never queued server-side (app/web/ingress.py rejects it before
    // any insert), so no SSE echo will ever arrive to reconcile this
    // entry -- leaving it in the queue would only risk a wrong match
    // against a later, unrelated message with the same text.
    removePendingOwn(pending);
    row.setNote('Эта команда доступна только в Telegram');
    return;
  }
  if (res.status === 429) {
    // A rate-limited send is not a lost cause -- unlike a 422, sending
    // the exact same text (and client_key, so a retry cannot duplicate
    // it) again later can succeed. Offering the retry button here,
    // instead of only a "wait N minutes" note with no way to act on it,
    // is what the composer's own UI text already promises. `pending`
    // stays in the queue (this attempt was never queued server-side
    // either, so nothing to reconcile yet) so the eventual successful
    // retry can still be reconciled by either race winner.
    row.setRetry(() => postSend(clientKey, text, row, pending));
    return;
  }
  row.setRetry(() => postSend(clientKey, text, row, pending));
}

function sendMessage(text) {
  const clientKey = crypto.randomUUID();
  const row = buildOutgoingRow(text);
  logEl.insertBefore(row.el, emptyStateEl);
  updateEmptyState();
  scrollToBottom();
  // Registered before postSend's first `await` (the fetch itself), so
  // it is in place no matter how fast the SSE echo of this same
  // message comes back -- see the `pendingOwn` comment above.
  const pending = { text, row: row.el, bubble: row.bubble };
  pendingOwn.push(pending);
  postSend(clientKey, text, row, pending);
}

function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = `${el.scrollHeight}px`;
}

composerInput.addEventListener('input', () => autoGrow(composerInput));

composerInput.addEventListener('keydown', (e) => {
  // Enter used to confirm an IME composition candidate (CJK and other
  // scripts) also submitted the half-composed text, because the
  // browser fires a plain keydown with key 'Enter' for that same
  // keystroke. isComposing (and keyCode 229 for the handful of
  // browsers that do not set it) is how a composing Enter is told
  // apart from a submitting one.
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key !== 'Enter' || e.shiftKey) return;
  // On touch devices Enter inserts a newline; the send button is the
  // only way to submit, matching the platform's own text-entry habits.
  if (window.matchMedia('(pointer: coarse)').matches) return;
  e.preventDefault();
  composerForm.requestSubmit();
});

composerForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = composerInput.value.trim();
  if (!text) return;
  composerInput.value = '';
  autoGrow(composerInput);
  sendMessage(text);
  composerInput.focus();
});

pauseButton.addEventListener('click', () => sendMessage('/out'));
logoutButton.addEventListener('click', logout);

// ---------------------------------------------------------------------
// Bootstrap.
// ---------------------------------------------------------------------

async function enterChat() {
  showChat();
  rendered.clear();
  logEl.querySelectorAll('.msg-row').forEach((n) => n.remove());
  oldestId = null;
  hasMoreHistory = true;
  historyReady = false;
  pendingLive = [];
  updateEmptyState();
  connectSSE();
  await loadHistory();
  historyReady = true;
  flushPendingLive();
  composerInput.focus();
}

async function init() {
  passphraseForm.addEventListener('submit', onPassphraseSubmit);
  codeForm.addEventListener('submit', onCodeSubmit);
  codeBackButton.addEventListener('click', () => setLoginStage('none'));

  const res = await apiGet('/api/me');
  if (res.ok && res.data && res.data.authenticated) {
    await enterChat();
  } else {
    setLoginStage(res.ok && res.data ? res.data.stage : 'none');
    showLogin();
  }
}

document.addEventListener('DOMContentLoaded', init);
