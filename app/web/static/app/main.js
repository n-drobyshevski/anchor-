// Bootstrap: mount the app, start the hash router, wire the SSE
// stream's lifecycle to the `auth` signal, then resolve the initial
// session state via GET /api/me. Replaces the old app.js's
// `document.addEventListener('DOMContentLoaded', init)`.
import { render, html } from './html.js';
import { effect } from '../vendor/signals.module.js';
import { App } from './ui/App.js';
import { start as startRouter } from './router.js';
import { apiGet } from './api.js';
import { auth } from './store.js';
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

async function init() {
  const res = await apiGet('/api/me');
  if (res.ok && res.data && res.data.authenticated) {
    auth.value = 'in';
  } else {
    auth.value = res.ok && res.data ? res.data.stage : 'none';
  }
}

init();
