// The toast region: an aria-live=polite status list, auto-dismissed by
// store.js's pushToast() (3.5s per toast, unchanged from the old
// app.js). Rendered exactly once, by ui/Shell.js, inside .shell-screen
// -- a non-scrolling positioned box, so app.css's `#toast-region {
// position: absolute; ... }` stays pinned instead of scrolling away
// with a screen's content. (Every screen used to render its own copy,
// so two #toast-region elements existed whenever you were off-chat,
// and a screen still loading showed none.)
import { html } from '../html.js';
import { toasts } from '../store.js';

export function Toasts() {
  return html`
    <div id="toast-region" role="status" aria-live="polite">
      ${toasts.value.map((t) => html`<div class="toast" key=${t.id}>${t.text}</div>`)}
    </div>
  `;
}
