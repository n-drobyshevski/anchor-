// Top-level screen switch, replacing the old app.js's
// showLogin()/showChat(). Reads nothing but store.js's `auth` signal;
// everything else (which login step, which chat state) lives further
// down the tree.
import { html } from '../html.js';
import { auth } from '../store.js';
import { Login } from './Login.js';
import { Shell } from './Shell.js';

export function App() {
  const stage = auth.value;
  // Before the bootstrap GET /api/me (main.js) resolves, render
  // nothing -- matching the old index.html, where both #login and
  // #chat started `hidden` until init() picked one.
  if (stage === 'unknown') return null;
  return stage === 'in' ? html`<${Shell} />` : html`<${Login} stage=${stage} />`;
}
