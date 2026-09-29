// Bootstrap: mount the app, start the hash router, wire the SSE
// stream's lifecycle to the `auth` signal, then resolve the initial
// session state via GET /api/me. Replaces the old app.js's
// `document.addEventListener('DOMContentLoaded', init)`.
import { render, html } from './html.js';
import { effect } from '../vendor/signals.module.js';
import { App } from './ui/App.js';
import { start as startRouter } from './router.js';
import { apiGet } from './api.js';
import { load } from './lib/request.js';
import { auth, invalidate, paused, proposalsBadge } from './store.js';
import { close as closeSSE, connect as connectSSE } from './sse.js';

startRouter();
render(html`<${App} />`, document.getElementById('root'));

// A plain (non-component) effect, not a hook: the SSE stream is
// app-wide, not owned by any one screen, so it starts and stops
// exactly with `auth` leaving/entering 'in' regardless of which
// component tree happens to be mounted at the time -- mirroring the
// old app.js's enterChat()/goToLogin() calling connectSSE()/closeSSE()
// directly. Runs once immediately (auth is still 'unknown', so this
// first call is just close()'s harmless no-op teardown) and again on
// every later change.
effect(() => {
  if (auth.value === 'in') connectSSE();
  else closeSSE();
});

// App-wide values that must stay right whichever screen is open (or
// none that shows them): store.js's proposalsBadge (the switcher's pip
// and the «Предложения» count -- a proposal raised while the user
// reads #/chat still has to show up) and `paused` (the toolbar's
// pause/resume button). Refetched right after login, on every
// invalidate of their topic ('*' is sse.js's reconnect resync, an
// invalidate this tab may have missed while the stream was down), and
// when a backgrounded tab comes back. screens/Proposals.js and
// screens/State.js also set them from their own loads, so neither
// waits on a second round-trip here when that screen is open.
async function refreshProposalsBadge() {
  const res = await load('/api/proposals');
  if (res.ok) proposalsBadge.value = res.data.pending ? 1 : 0;
}

async function refreshPaused() {
  const res = await load('/api/state');
  if (res.ok) paused.value = !!res.data.paused;
}

function refreshAll() {
  refreshProposalsBadge();
  refreshPaused();
}

effect(() => {
  if (auth.value === 'in') refreshAll();
  else paused.value = null;
});

// A plain subscription rather than an effect over both signals: an
// effect reading `auth` too would also rerun on login, doubling the
// fetch refreshAll() above already makes. `subscribe` calls back once
// immediately with the last invalidate (skipped) -- only later ones
// count.
let skipFirstInvalidate = true;
invalidate.subscribe((current) => {
  if (skipFirstInvalidate) {
    skipFirstInvalidate = false;
    return;
  }
  const topic = current && current.topic;
  if (auth.peek() !== 'in' || !topic) return;
  if (topic === 'proposals' || topic === '*') refreshProposalsBadge();
  if (topic === 'state' || topic === '*') refreshPaused();
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && auth.value === 'in') refreshAll();
});

async function init() {
  const res = await apiGet('/api/me');
  if (res.ok && res.data && res.data.authenticated) {
    auth.value = 'in';
  } else {
    auth.value = res.ok && res.data ? res.data.stage : 'none';
  }
}

init();
