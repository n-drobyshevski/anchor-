// The two states every panel screen shows before it has data: loading
// (an empty, aria-busy screen) and a first load that failed (a retry
// link). `wide` matches a screen that uses .screen-wrap-wide.
import { html } from '../html.js';

export function ScreenLoading({ wide = false } = {}) {
  return html`
    <div class="screen-wrap${wide ? ' screen-wrap-wide' : ''}">
      <div class="screen" aria-busy="true"></div>
    </div>
  `;
}

export function ScreenError({ onRetry, wide = false }) {
  return html`
    <div class="screen-wrap${wide ? ' screen-wrap-wide' : ''}">
      <div class="screen">
        <${LoadError} onRetry=${onRetry} />
      </div>
    </div>
  `;
}

// The same message inside a section or list, for a load that failed
// after the screen itself had already rendered.
export function LoadError({ onRetry }) {
  return html`
    <p class="empty-hint" role="alert">
      Не удалось загрузить.
      <button type="button" class="link-button" onClick=${onRetry}>Повторить</button>
    </p>
  `;
}
