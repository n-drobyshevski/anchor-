// The toast region: an aria-live=polite status list, auto-dismissed by
// store.js's pushToast() (3.5s per toast, unchanged from the old
// app.js). Rendered inside a non-scrolling positioned ancestor -- #chat
// itself (screens/Chat.js) or a screen's own `.screen-wrap`
// (screens/State.js, screens/Proposals.js) -- never inside the
// scrolling element itself, so app.css's `#toast-region { position:
// absolute; ... }` stays pinned to the viewport instead of scrolling
// out of view with the content.
import { html } from '../html.js';
import { toasts } from '../store.js';

export function Toasts() {
  return html`
    <div id="toast-region" role="status" aria-live="polite">
      ${toasts.value.map((t) => html`<div class="toast" key=${t.id}>${t.text}</div>`)}
    </div>
  `;
}
