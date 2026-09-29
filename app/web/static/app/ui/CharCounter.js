// «n/max» under a text field, as a polite live region so a screen
// reader hears it cross the limit. Counts what will be sent: callers
// pass the trimmed length when the backend trims.
import { html } from '../html.js';

export function CharCounter({ length, max, id }) {
  const over = length > max;
  return html`
    <p id=${id} class="char-counter${over ? ' char-counter-over' : ''}" aria-live="polite">${length}/${max}</p>
  `;
}
