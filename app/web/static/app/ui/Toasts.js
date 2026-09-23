// The toast region: an aria-live=polite status list, auto-dismissed by
// store.js's pushToast() (3.5s per toast, unchanged from the old
// app.js). Rendered inside screens/Chat.js's #chat, not at the app
// root, so app.css's `#toast-region { position: absolute; ... }` still
// resolves against #chat's `position: relative`, exactly as before.
import { html } from '../html.js';
import { toasts } from '../store.js';

export function Toasts() {
  return html`
    <div id="toast-region" role="status" aria-live="polite">
      ${toasts.value.map((t) => html`<div class="toast" key=${t.id}>${t.text}</div>`)}
    </div>
  `;
}
