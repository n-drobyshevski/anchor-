// The chat screen: every behavior from the old app.js's chat section,
// ported onto Preact. The message log itself is *not* Preact state
// (`useState`) -- it is a plain mutable array behind a ref, exactly
// like the old `rendered` Map, with a `useReducer` counter used only
// to force a re-render after each mutation. That choice is deliberate:
// this screen's whole design is about getting insertion order, dedupe
// and scroll position exactly right across two independent, racing
// event sources (GET /api/history and the SSE stream) -- state that
// must be read and written synchronously, in one place, the instant an
// event arrives. Routing it through Preact's async, batched
// `setState` would reopen exactly the ordering races the old app.js's
// comments (ported below, unchanged) spent so much care closing.
import { html } from '../html.js';
import { useEffect, useLayoutEffect, useReducer, useRef, useState } from '../../vendor/hooks.module.js';
import { apiGet, apiPost } from '../api.js';
import * as sse from '../sse.js';
import { forceLogout, pushToast, reconnectBanner, registerPauseHandler, typing } from '../store.js';
import { Toasts } from '../ui/Toasts.js';

function minutesText(retryAfterSeconds) {
  const m = Math.max(1, Math.ceil((retryAfterSeconds || 60) / 60));
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

function formatTime(ts) {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return { short: `${hh}:${mm}`, full: d.toLocaleString('ru-RU') };
}

// These two read their own signal and nothing else -- module-level,
// same reasoning as Keyboard below, plus a reactivity reason of its
// own: a Preact/Signals component that reads a signal's `.value` in
// its render body subscribes *that component* to it, so it alone
// re-renders on every change. Chat() itself used to read conn.value/
// reconnectBanner.value/typing.value directly, which meant every
// typing tick (twice per 6 s while the bot composes) or connection
// blip re-ran the whole screen -- remapping every loaded message row
// (up to 1000+ after paging) into fresh MessageRow elements and
// re-running formatTime for each just to re-diff a status dot. Moving
// the read down here confines that re-render to one <div>/<p>. (The
// connection dot, the third of these, now lives in ui/Toolbar.js.)
function ReconnectBanner() {
  return html`
    <div id="reconnect-banner" role="alert" hidden=${!reconnectBanner.value}>
      Нет соединения. Переподключаемся…
    </div>
  `;
}

function TypingIndicator() {
  return html`<p id="typing-indicator" hidden=${!typing.value}>печатает…</p>`;
}

// Module-level (not inside Chat()) so its identity is stable across
// renders -- a component redefined on every render would make Preact
// remount every row every time instead of diffing them.
function Keyboard({ rows, disabled, onPress }) {
  return html`
    <div class="keyboard">
      ${rows.map(
        (row, i) => html`
          <div class="keyboard-row" key=${i}>
            ${row.map(
              (btn, j) => html`
                <button
                  key=${j}
                  type="button"
                  class="keyboard-button"
                  disabled=${disabled}
                  onClick=${() => onPress(btn.data)}
                >
                  ${btn.text}
                </button>
              `,
            )}
          </div>
        `,
      )}
    </div>
  `;
}

function MessageRow({ row, onPress }) {
  const time = row.own ? null : formatTime(row.ts);
  return html`
    <div class="msg-row role-${row.role}" data-kind=${row.kind || undefined}>
      <div class="bubble">${row.text}</div>
      ${row.keyboard
        ? html`<${Keyboard}
            rows=${row.keyboard}
            disabled=${row.keyboardDisabled}
            onPress=${(data) => onPress(row, data)}
          />`
        : null}
      ${row.own
        ? html`
            <div class=${row.footerMode === 'status' ? 'msg-status' : 'msg-note'}>
              ${row.footerMode === 'retry'
                ? html`<button type="button" class="msg-retry" onClick=${row.retry}>
                    Не отправлено — повторить
                  </button>`
                : row.footerText}
            </div>
          `
        : html` <div class="msg-time" title=${time.full}>${time.short}</div> `}
    </div>
  `;
}

// `hidden`: ui/Shell.js renders Chat unconditionally (never unmounting
// it on a tab switch, unlike State/Proposals) and toggles this instead
// -- see Shell.js's own comment for why. `hidden` on the root element
// hides it via app.css's `[hidden] { display: none !important; }`
// while every ref, timer and SSE subscription below keeps running, so
// switching back to #/chat needs no re-fetch and loses no scroll
// position or composer draft, and a live SSE event that arrives while
// the tab is elsewhere is still rendered into the log (just unseen)
// instead of racing a fresh remount's history load -- the ordering bug
// a per-tab mount/unmount used to reopen every time nav grew past one
// screen.
export function Chat({ hidden = false } = {}) {
  const logRef = useRef(null);
  const topSentinelRef = useRef(null);
  const composerFormRef = useRef(null);
  const composerRef = useRef(null);

  const [, bump] = useReducer((n) => n + 1, 0);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [jumpDownVisible, setJumpDownVisible] = useState(false);

  // id (DB row id, or the sink's negative id) -> nothing; membership
  // alone backs dedupe, exactly like the old `rendered` Map's `.has`.
  const idSetRef = useRef(new Set());
  // Ordered oldest-first; the single source of truth the log renders
  // from. Mutated in place, then `bump()`.
  const messagesRef = useRef([]);
  // FIFO of {text, row} for this tab's own outgoing messages not yet
  // matched to a server id -- see claimPendingOwn()'s comment below.
  const pendingOwnRef = useRef([]);
  const nextOwnKeyRef = useRef(0);

  const oldestIdRef = useRef(null);
  const hasMoreRef = useRef(true);
  const historyLoadingRef = useRef(false);

  // What the post-render scroll effect below should do, set by
  // whichever mutation just ran; consumed and cleared every render.
  const pendingScrollRef = useRef(null);
  const preserveScrollInfoRef = useRef(null);

  function isNearBottom() {
    const log = logRef.current;
    if (!log) return true;
    return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  }

  function claimPendingOwn(text) {
    const q = pendingOwnRef.current;
    const i = q.findIndex((p) => p.text === text);
    return i === -1 ? null : q.splice(i, 1)[0];
  }

  function removePendingOwn(entry) {
    const q = pendingOwnRef.current;
    const i = q.indexOf(entry);
    if (i !== -1) q.splice(i, 1);
  }

  function buildServerRow(msg) {
    return {
      key: `srv:${msg.id}`,
      id: msg.id,
      role: msg.role,
      kind: msg.kind,
      text: msg.text,
      ts: msg.ts,
      keyboard: msg.keyboard || null,
      keyboardDisabled: false,
      own: false,
    };
  }

  // A live message arriving over SSE (already past sse.js's
  // history-load buffering gate): append at the bottom, auto-scroll
  // only if the reader was already near the bottom, otherwise surface
  // the "new messages" pill instead of yanking their scroll position.
  function renderIncoming(msg) {
    if (idSetRef.current.has(msg.id)) return;
    if (msg.role === 'user') {
      // The server's echo of a message this tab itself just sent --
      // adopt the existing optimistic row under this id instead of
      // building a disconnected second one. A message a *different*
      // tab/device sent has no matching pending entry and falls
      // through to the ordinary render below.
      const own = claimPendingOwn(msg.text);
      if (own) {
        own.row.id = msg.id;
        idSetRef.current.add(msg.id);
        bump();
        return;
      }
    }
    const wasNear = isNearBottom();
    idSetRef.current.add(msg.id);
    messagesRef.current.push(buildServerRow(msg));
    pendingScrollRef.current = wasNear ? 'bottom' : 'pill';
    bump();
  }

  function applyEdit(edit) {
    const row = messagesRef.current.find((r) => r.id === edit.id);
    if (!row) return;
    if ('text' in edit && edit.text != null) row.text = edit.text;
    if ('keyboard' in edit) {
      row.keyboard = edit.keyboard || null;
      row.keyboardDisabled = false;
    }
    bump();
  }

  async function handlePress(row, data) {
    // Captured before the await: an SSE `edit` can replace row.keyboard
    // with a brand-new one while this press is in flight (the server
    // already updated its allowlist when it does, so this press's
    // eventual 409 belongs to the *old* keyboard only). Every mutation
    // below is guarded by `row.keyboard !== kb` so a response that
    // arrives after the row has moved on to a different keyboard can
    // never touch it -- matching the old app.js, which only ever called
    // keyboardEl.remove()/enabled toggles on the exact <div> it built
    // the click handler for, never on whatever the row currently holds.
    const kb = row.keyboard;
    row.keyboardDisabled = true;
    bump();
    const res = await apiPost('/api/press', { message_id: row.id, data });
    if (res.status === 202) return; // stays disabled until an `edit` (or reload) replaces it
    if (res.status === 401) {
      forceLogout();
      return;
    }
    const stale = row.keyboard !== kb;
    if (res.status === 409) {
      // Stale: the allowlist for this message moved on. Re-enabling
      // let the user keep pressing buttons the server would only ever
      // reject again with another 409, one toast at a time. Removing
      // the keyboard instead makes the one rejection the last one --
      // but only when this is still the keyboard the 409 is about; a
      // newer one an `edit` already installed must survive untouched.
      if (!stale) {
        row.keyboard = null;
        bump();
      }
      pushToast('устарело');
      return;
    }
    // Only a network/5xx/429 failure -- something that might succeed on
    // a retry -- re-enables the buttons, and only if this is still the
    // same keyboard: re-enabling a newer one mid-flight (its own press
    // already in progress) would allow a double press.
    if (!stale) {
      row.keyboardDisabled = false;
      bump();
    }
    if (res.status === 429) pushToast(minutesText(res.data && res.data.retry_after));
    else pushToast('Не отправлено.');
  }

  function insertHistoryPage(messages, initial) {
    const newRows = [];
    for (const m of messages) {
      if (idSetRef.current.has(m.id)) continue;
      idSetRef.current.add(m.id);
      newRows.push(buildServerRow(m));
    }
    if (!newRows.length) return;
    if (initial) {
      messagesRef.current.push(...newRows);
      pendingScrollRef.current = 'bottom-instant';
    } else {
      preserveScrollInfoRef.current = logRef.current
        ? { prevHeight: logRef.current.scrollHeight, prevTop: logRef.current.scrollTop }
        : null;
      messagesRef.current.unshift(...newRows);
      pendingScrollRef.current = 'preserve';
    }
    bump();
  }

  // A page of /api/history results, oldest-first. `initial` is the
  // very first page (append, then scroll down); every later page is
  // older messages loaded by scrolling up (prepend, preserving the
  // reader's scroll position).
  async function loadHistory() {
    if (historyLoadingRef.current || !hasMoreRef.current) return;
    historyLoadingRef.current = true;
    // `#log` is `aria-live="polite"`, and a history page can insert up
    // to 50 rows at once. `aria-busy` suppresses live-region
    // announcements for the region while it is set, so only a live
    // renderIncoming row -- never a history page -- gets read aloud.
    setHistoryBusy(true);
    try {
      const initial = oldestIdRef.current === null;
      const qs = initial ? 'limit=50' : `before=${oldestIdRef.current}&limit=50`;
      const res = await apiGet(`/api/history?${qs}`);
      if (res.status === 401) {
        forceLogout();
        return;
      }
      if (!res.ok || !res.data) return;
      const { messages, has_more } = res.data;
      hasMoreRef.current = !!has_more;
      if (messages.length) {
        oldestIdRef.current = messages[0].id;
        insertHistoryPage(messages, initial);
      } else if (initial) {
        oldestIdRef.current = -1; // no history at all; stop the sentinel from refetching forever
        bump(); // lets the empty-state paragraph show
      }
    } finally {
      historyLoadingRef.current = false;
      setHistoryBusy(false);
    }
  }

  async function postSend(clientKey, text, row, pending) {
    row.footerMode = 'status';
    row.footerText = 'отправка…';
    bump();
    const res = await apiPost('/api/send', { text, client_key: clientKey });
    if (res.status === 401) {
      removePendingOwn(pending);
      forceLogout();
      return;
    }
    if (res.status === 202) {
      row.footerMode = 'status';
      row.footerText = '';
      // The server also mirrors this message to every live SSE stream
      // (including this tab's own), so a second open tab/device can
      // show it too -- but that means this tab's own EventSource may
      // already have received (renderIncoming, via pendingOwn) or may
      // still be about to receive an echo of the very message it just
      // optimistically rendered. Registering it under the id the
      // server used covers the case where this response won the race:
      // idSetRef already has it, so if the echo arrives after this,
      // renderIncoming's ordinary dedupe skips it. removePendingOwn
      // covers the other case -- the echo already won and reconciled
      // `pending` itself -- by clearing the now-stale queue entry so
      // it cannot wrongly match a later, unrelated message with the
      // same text.
      if (res.data && typeof res.data.update_id === 'number') {
        row.id = res.data.update_id;
        idSetRef.current.add(res.data.update_id);
      }
      removePendingOwn(pending);
      bump();
      return;
    }
    if (res.status === 422) {
      // Never queued server-side, so no SSE echo will ever arrive to
      // reconcile this entry -- leaving it in the queue would only
      // risk a wrong match against a later, unrelated message with the
      // same text.
      removePendingOwn(pending);
      row.footerMode = 'note';
      row.footerText = 'Эта команда доступна только в Telegram';
      bump();
      return;
    }
    if (res.status === 429) {
      // A rate-limited send is not a lost cause -- sending the exact
      // same text (and client_key, so a retry cannot duplicate it)
      // again later can succeed. `pending` stays in the queue (this
      // attempt was never queued server-side either) so the eventual
      // successful retry can still be reconciled by either race
      // winner.
      row.footerMode = 'retry';
      row.retry = () => postSend(clientKey, text, row, pending);
      bump();
      return;
    }
    // Only a network/5xx failure reaches here.
    row.footerMode = 'retry';
    row.retry = () => postSend(clientKey, text, row, pending);
    bump();
  }

  function sendMessage(text) {
    const clientKey = crypto.randomUUID();
    const row = {
      key: `own:${nextOwnKeyRef.current++}`,
      id: null,
      role: 'user',
      own: true,
      text,
      footerMode: 'status',
      footerText: '',
    };
    messagesRef.current.push(row);
    pendingScrollRef.current = 'bottom';
    // Registered before postSend's first `await` (the fetch itself),
    // so it is in place no matter how fast the SSE echo of this same
    // message comes back -- see claimPendingOwn()'s comment above.
    const pending = { text, row };
    pendingOwnRef.current.push(pending);
    bump();
    postSend(clientKey, text, row, pending);
  }

  function autoGrow(el) {
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }

  function onComposerInput() {
    autoGrow(composerRef.current);
  }

  function onComposerKeyDown(e) {
    // Enter used to confirm an IME composition candidate (CJK and
    // other scripts) also submitted the half-composed text, because
    // the browser fires a plain keydown with key 'Enter' for that same
    // keystroke. isComposing (and keyCode 229 for the handful of
    // browsers that do not set it) is how a composing Enter is told
    // apart from a submitting one.
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key !== 'Enter' || e.shiftKey) return;
    // On touch devices Enter inserts a newline; the send button is the
    // only way to submit, matching the platform's own text-entry
    // habits.
    if (window.matchMedia('(pointer: coarse)').matches) return;
    e.preventDefault();
    composerFormRef.current.requestSubmit();
  }

  function onComposerSubmit(e) {
    e.preventDefault();
    const text = composerRef.current.value.trim();
    if (!text) return;
    composerRef.current.value = '';
    autoGrow(composerRef.current);
    sendMessage(text);
    composerRef.current.focus();
  }

  function handleJumpDown() {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
    setJumpDownVisible(false);
  }

  // The toolbar's #pause-button (ui/Toolbar.js) sends `/out` through
  // this screen's own sendMessage, exactly as #chat-header's button
  // did: store.js's requestPause() calls whatever is registered here.
  // Through a ref, so the handler is always this render's sendMessage.
  const sendMessageRef = useRef(sendMessage);
  sendMessageRef.current = sendMessage;
  useEffect(() => registerPauseHandler(() => sendMessageRef.current('/out')), []);

  // enterChat(): resets every per-session piece of state and starts
  // loading history. Runs once per mount, i.e. once per login (Shell
  // only renders Chat while auth === 'in').
  useEffect(() => {
    messagesRef.current = [];
    idSetRef.current = new Set();
    pendingOwnRef.current = [];
    oldestIdRef.current = null;
    hasMoreRef.current = true;
    // No sse.resetHistoryGate() call here (unlike the gate's own
    // module comment for why it exists): main.js's `auth` effect calls
    // sse.close() -- which now resets the gate itself -- synchronously
    // on every login/logout transition, strictly before this mount
    // effect can run. Resetting it again here would throw away exactly
    // the events sse.js buffered in the window between that connect()
    // and this mount, which is the opposite of what the gate is for.
    bump();
    const unsubMessage = sse.onMessage(renderIncoming);
    const unsubEdit = sse.onEdit(applyEdit);
    // Guards markHistoryReady() against a Chat that unmounts (a logout
    // during the initial history fetch) before loadHistory() resolves --
    // without it, a stale mount's IIFE would flip the gate open and
    // replay its buffered events into whatever screen/session is
    // current by the time the awaited fetch finally returns.
    let cancelled = false;
    (async () => {
      await loadHistory();
      if (cancelled) return;
      sse.markHistoryReady();
      if (composerRef.current) composerRef.current.focus();
    })();
    return () => {
      cancelled = true;
      unsubMessage();
      unsubEdit();
    };
    // eslint-disable-next-line
  }, []);

  useEffect(() => {
    const log = logRef.current;
    if (!log) return undefined;
    const onScroll = () => {
      if (isNearBottom()) setJumpDownVisible(false);
    };
    log.addEventListener('scroll', onScroll);
    return () => log.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    const sentinel = topSentinelRef.current;
    const log = logRef.current;
    if (!sentinel || !log) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) loadHistory();
      },
      { root: log, threshold: 0 },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
    // eslint-disable-next-line
  }, []);

  // Runs after every commit; only acts when a mutation above actually
  // set pendingScrollRef, so it is a no-op on renders that touch
  // nothing scroll-related (a keyboard press, a footer text change).
  useLayoutEffect(() => {
    const mode = pendingScrollRef.current;
    pendingScrollRef.current = null;
    const log = logRef.current;
    if (!mode || !log) return;
    if (mode === 'bottom' || mode === 'bottom-instant') {
      log.scrollTop = log.scrollHeight;
      setJumpDownVisible(false);
    } else if (mode === 'pill') {
      setJumpDownVisible(true);
    } else if (mode === 'preserve') {
      const info = preserveScrollInfoRef.current;
      preserveScrollInfoRef.current = null;
      if (info) log.scrollTop = info.prevTop + (log.scrollHeight - info.prevHeight);
    }
  });

  // Never shown while the initial history fetch is still in flight
  // (oldestIdRef is still null then) -- only once it has resolved,
  // either to "genuinely no history" (oldestIdRef set to -1) or to a
  // real oldest id, at which point an empty log really does mean
  // empty. Matches the old app.js, where #empty-state started
  // `hidden` and was only ever unhidden by updateEmptyState() after a
  // page landed, never during the fetch itself.
  const showEmpty = messagesRef.current.length === 0 && oldestIdRef.current !== null;

  return html`
    <div id="chat" hidden=${hidden}>
      <${ReconnectBanner} />

      <main id="log" role="log" aria-live="polite" aria-relevant="additions" ref=${logRef} aria-busy=${historyBusy ? 'true' : 'false'}>
        <div id="top-sentinel" ref=${topSentinelRef}></div>
        ${messagesRef.current.map(
          (row) => html`<${MessageRow} key=${row.key} row=${row} onPress=${handlePress} />`,
        )}
        <p id="empty-state" hidden=${!showEmpty}>Напиши что-нибудь. /out — пауза, /in — вернуться.</p>
      </main>

      <${TypingIndicator} />

      <button type="button" id="jump-down" class="jump-pill" hidden=${!jumpDownVisible} onClick=${handleJumpDown}>
        ↓ Новые
      </button>

      <${Toasts} />

      <form id="composer" ref=${composerFormRef} onSubmit=${onComposerSubmit}>
        <textarea
          id="composer-input"
          ref=${composerRef}
          rows="1"
          placeholder="Сообщение"
          maxlength="4000"
          required
          autocomplete="off"
          onInput=${onComposerInput}
          onKeyDown=${onComposerKeyDown}
        ></textarea>
        <button type="submit" id="send-button" aria-label="Отправить">
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false">
            <path
              d="M3 11.5 20.5 4 13 21.5l-2.2-7.3L3 11.5Z"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linejoin="round"
              stroke-linecap="round"
            />
          </svg>
        </button>
      </form>
    </div>
  `;
}
