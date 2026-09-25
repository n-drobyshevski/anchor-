// The one top toolbar every screen shares (after Planner's
// SurfaceChrome), at every width: [switcher] [title] ... [Пауза][Выйти].
// Replaces both the old bottom tab bar / left sidebar (ui/Nav.js) and
// Chat's own #chat-header, so #pause-button and #logout-button now live
// here, and the page's single <h1> is the toolbar title.
//
// Title block: on #/chat, "Anchor" with the SSE connection state under
// it (a dot plus its text label -- never colour alone); on every other
// screen, that screen's name plus an optional subtitle a screen can set
// through store.js's screenSubtitle.
import { html } from '../html.js';
import { conn, logout, requestPause, screenSubtitle } from '../store.js';
import { Icon } from './Icon.js';
import { SurfaceSwitcher, currentNavItem } from './SurfaceSwitcher.js';

const CONN_LABELS = { ok: 'на связи', reconnecting: 'переподключение', down: 'нет связи' };

// Reads `conn` on its own, so a connection blip re-renders this line
// only, not the toolbar or the chat log.
function ConnStatus() {
  return html`
    <span class="toolbar-subtitle conn-status">
      <span id="conn-dot" class="conn-dot" data-state=${conn.value} aria-hidden="true"></span>
      <span>${CONN_LABELS[conn.value]}</span>
    </span>
  `;
}

function Subtitle() {
  const text = screenSubtitle.value;
  return text ? html`<span class="toolbar-subtitle">${text}</span>` : null;
}

export function Toolbar() {
  const item = currentNavItem();
  const isChat = item.route === '#/chat';
  return html`
    <header id="toolbar" class="toolbar">
      <${SurfaceSwitcher} />
      <div class="toolbar-title">
        <h1>${isChat ? 'Anchor' : item.label}</h1>
        ${isChat ? html`<${ConnStatus} />` : html`<${Subtitle} />`}
      </div>
      <div class="toolbar-trailing">
        <button
          type="button"
          id="pause-button"
          class="icon-button"
          aria-label="Пауза"
          title="Пауза"
          onClick=${requestPause}
        >
          <${Icon} name="pause" size=${18} />
        </button>
        <button
          type="button"
          id="logout-button"
          class="icon-button"
          aria-label="Выйти"
          title="Выйти"
          onClick=${logout}
        >
          <${Icon} name="log-out" size=${18} />
        </button>
      </div>
    </header>
  `;
}
