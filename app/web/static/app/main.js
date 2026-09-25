// Bootstrap: mount the app, start the hash router, wire the SSE
// stream's lifecycle to the `auth` signal, then resolve the initial
// session state via GET /api/me. Replaces the old app.js's
// `document.addEventListener('DOMContentLoaded', init)`.
import { render, html } from './html.js';
import { effect } from '../vendor/signals.module.js';
import { App } from './ui/App.js';
import { start as startRouter } from './router.js';
import { apiGet } from './api.js';
import { auth, invalidate, proposalsBadge } from './store.js';
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

// store.js's proposalsBadge (ui/SurfaceSwitcher.js's pip and the
// «Предложения» count) has to stay right even while #/proposals itself
// is never opened -- a proposal raised while the user reads #/chat
// still needs to show up in the switcher. This effect is that path: refetch right after login,
// and again on every invalidate("proposals"), independent of which
// screen (if any) is mounted. screens/Proposals.js keeps its own copy
// of this same call so the badge does not wait on this effect's own
// round-trip when that screen is the one already fetching.
async function refreshProposalsBadge() {
  const res = await apiGet('/api/proposals');
  if (res.ok && res.data) proposalsBadge.value = res.data.pending ? 1 : 0;
}

effect(() => {
  if (auth.value === 'in') refreshProposalsBadge();
});

effect(() => {
  const current = invalidate.value;
  // '*' is sse.js's SSE-reconnect resync (an invalidate this tab may
  // have missed entirely while the stream was down) -- treated the
  // same as a direct "proposals" hit, same reasoning as
  // hooks.js's useAutoRefetch.
  const topic = current && current.topic;
  if (auth.value === 'in' && (topic === 'proposals' || topic === '*')) refreshProposalsBadge();
});

// The badge can also go stale with no SSE event at all: the stream
// stays connected throughout, but this tab is backgrounded (another
// app in front, the OS suspends the tab) for long enough that
// whichever event arrived was never acted on the way a foregrounded
// tab's screens act on it via useAutoRefetch's own visibilitychange
// listener -- this is that same recovery for the badge, which is never
// owned by a mounted screen when #/proposals itself is not open.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && auth.value === 'in') refreshProposalsBadge();
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
