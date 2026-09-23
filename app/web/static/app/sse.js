// EventSource lifecycle: connect/reconnect with backoff, the
// /api/me-on-error recheck, the history-load buffering gate, and
// fan-out of message/edit/typing/toast/invalidate to whoever
// subscribed. Ported 1:1 from the old app.js's SSE section; the only
// structural change is that "what happens with a message/edit" is no
// longer hard-coded here -- screens/Chat.js subscribes via onMessage/
// onEdit instead of this module reaching into the DOM directly.
import { apiGet } from './api.js';
import { conn, forceLogout, invalidate, pushToast, reconnectBanner, typing } from './store.js';

let es = null;
let sseErrorCount = 0;
let reconnectTimer = null;
let typingTimer = null;

// Buffers 'message'/'edit' events that arrive before the subscribing
// screen's own initial history page has finished loading -- see
// resetHistoryGate()/markHistoryReady() below, and the identical
// comment this is ported from in the old app.js, for why this exists
// (the SSE echo of this tab's own outgoing message routinely arrives
// before GET /api/history's response does, and rendering it immediately
// would insert it *above* the history page that is supposed to precede
// it once that page lands).
let historyReady = false;
let pendingLive = [];

const messageListeners = new Set();
const editListeners = new Set();

function dispatchMessage(msg) {
  for (const cb of messageListeners) cb(msg);
}

function dispatchEdit(edit) {
  for (const cb of editListeners) cb(edit);
}

// Subscribe to live 'message'/'edit' events (already gated past the
// history-load buffer below). Returns an unsubscribe function.
export function onMessage(cb) {
  messageListeners.add(cb);
  return () => messageListeners.delete(cb);
}

export function onEdit(cb) {
  editListeners.add(cb);
  return () => editListeners.delete(cb);
}

// Called once per login (Chat.js's mount), before it starts loading
// history, so nothing buffered from a previous session lingers.
export function resetHistoryGate() {
  historyReady = false;
  pendingLive = [];
}

// Called once the subscribing screen's initial history page has
// landed: flips the gate and replays anything that arrived while it
// was loading, oldest first.
export function markHistoryReady() {
  historyReady = true;
  const buffered = pendingLive;
  pendingLive = [];
  for (const item of buffered) {
    if (item.kind === 'message') dispatchMessage(item.msg);
    else dispatchEdit(item.edit);
  }
}

function teardownEventSource() {
  if (es) {
    es.removeEventListener('error', onSSEError);
    es.close();
    es = null;
  }
}

export function connect() {
  teardownEventSource();
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  es = new EventSource('/api/events');
  es.addEventListener('open', () => {
    // An invalidate published while this stream was down (an outage,
    // the reader's laptop asleep, ...) reaches no subscriber -- SSE
    // has no replay/backlog. `'*'` tells every useAutoRefetch() (and
    // main.js's own proposals-badge listener below, which treats it
    // the same way) to resync unconditionally, the same recovery an
    // ordinary tab-visibility change already gives a *mounted* screen;
    // this is what covers one that was never mounted, or a signal
    // whose screen was not open, during the drop. Gated on
    // `sseErrorCount > 0` so an ordinary first connect-on-login (no
    // prior error) does not fire a resync nothing has gone stale for.
    if (sseErrorCount > 0) invalidate.value = { topic: '*' };
    sseErrorCount = 0;
    conn.value = 'ok';
    reconnectBanner.value = false;
  });
  es.addEventListener('error', onSSEError);
  es.addEventListener('message', (ev) => {
    hideTyping();
    const msg = JSON.parse(ev.data);
    if (!historyReady) {
      pendingLive.push({ kind: 'message', msg });
      return;
    }
    dispatchMessage(msg);
  });
  es.addEventListener('edit', (ev) => {
    const edit = JSON.parse(ev.data);
    if (!historyReady) {
      pendingLive.push({ kind: 'edit', edit });
      return;
    }
    dispatchEdit(edit);
  });
  es.addEventListener('typing', showTyping);
  es.addEventListener('toast', (ev) => pushToast(JSON.parse(ev.data).text));
  es.addEventListener('invalidate', (ev) => {
    invalidate.value = { topic: JSON.parse(ev.data).topic };
  });
}

// EventSource's own automatic reconnect only fires while the browser
// considers the connection recoverable; a non-200 response (a 429 from
// the 3-stream cap, or a 502/503 from a proxy mid-deploy) instead moves
// it straight to CLOSED, permanently, with no further retry of its own.
// This is what restarts it in that case, with capped exponential
// backoff -- sseErrorCount is deliberately *not* reset by a scheduled
// reconnect (only a successful 'open', or an explicit close(), resets
// it), so the backoff and the reconnect banner both keep accounting for
// the whole outage rather than restarting their count on every attempt.
function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = Math.min(30000, 1000 * 2 ** Math.min(sseErrorCount, 5));
  reconnectTimer = setTimeout(connect, delay);
}

async function onSSEError() {
  sseErrorCount += 1;
  conn.value = 'reconnecting';
  if (sseErrorCount >= 3) reconnectBanner.value = true;
  if (es && es.readyState === EventSource.CLOSED) {
    scheduleReconnect();
  }
  // Only a server-confirmed "you are not logged in" sends the app back
  // to the login screen. A network failure (status 0) or a 5xx/502
  // during a deploy both make /api/me's own fetch fail or come back
  // non-200 too -- treating *that* as "log out" would throw away a
  // perfectly good session on the very first blip, before the
  // reconnect banner above even had a chance to show.
  const res = await apiGet('/api/me');
  if (res.status === 401 || (res.status === 200 && res.data && res.data.authenticated === false)) {
    forceLogout();
  }
}

export function close() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  teardownEventSource();
  sseErrorCount = 0;
  reconnectBanner.value = false;
  conn.value = 'down';
  // Reset the history-load gate here too, not only in resetHistoryGate()
  // (which Chat's mount effect no longer calls -- see that effect's own
  // comment): on logout the gate is otherwise left open from the
  // previous session, and connect() on the next login can start
  // delivering live events before the freshly mounted Chat has
  // subscribed at all, losing them to an empty listener set instead of
  // buffering them for the mount that is about to happen.
  historyReady = false;
  pendingLive = [];
}

function showTyping() {
  typing.value = true;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(hideTyping, 6000);
}

function hideTyping() {
  typing.value = false;
  clearTimeout(typingTimer);
}
