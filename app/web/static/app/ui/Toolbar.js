// The one top toolbar every screen shares (after Planner's
// SurfaceChrome), at every width: [switcher] [title] ... [Пауза][Выйти].
// Replaces both the old bottom tab bar / left sidebar (ui/Nav.js) and
// Chat's own #chat-header, so #pause-button and #logout-button now live
// here, and the page's single <h1> is the toolbar title.
//
// #pause-button pauses or resumes, whichever store.js's `paused` says
// applies, through POST /api/state/pause -- the same endpoint as the
// State screen's switch (it used to send `/out` through Chat and could
// only pause).
//
// Title block: on #/chat, "Echo" with the SSE connection state under
// it (a dot plus its text label -- never colour alone); on every other
// screen, that screen's name plus an optional subtitle a screen can set
// through store.js's screenSubtitle.
import { html } from '../html.js';
import { useEffect, useRef, useState } from '../../vendor/hooks.module.js';
import { conn, logout, paused, pushToast, screenSubtitle } from '../store.js';
import { send } from '../lib/request.js';
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

// The 202 is only "queued": `paused` catches up once the turn pipeline
// applies /out or /in and invalidate("state") refetches it. Until then
// (or this safety window) the button stays disabled, so a second tap
// cannot queue the same command twice.
const PENDING_CLEAR_MS = 15000;

function PauseButton() {
  const value = paused.value;
  const [pending, setPending] = useState(null);
  const timeoutRef = useRef(null);

  useEffect(() => {
    if (pending !== null && value === pending) {
      clearTimeout(timeoutRef.current);
      setPending(null);
    }
  }, [value, pending]);
  useEffect(() => () => clearTimeout(timeoutRef.current), []);

  const resume = value === true;
  const label = resume ? 'Продолжить' : 'Пауза';

  async function toggle() {
    const target = !resume;
    setPending(target);
    const res = await send('/api/state/pause', { on: target });
    if (!res.ok) {
      setPending(null);
      pushToast(res.error);
      return;
    }
    pushToast(target ? 'Пауза.' : 'Пауза снята.');
    clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setPending(null), PENDING_CLEAR_MS);
  }

  return html`
    <button
      type="button"
      id="pause-button"
      class="icon-button"
      aria-label=${label}
      title=${label}
      aria-busy=${pending !== null ? 'true' : 'false'}
      disabled=${pending !== null}
      onClick=${toggle}
    >
      <${Icon} name=${resume ? 'play' : 'pause'} size=${18} />
    </button>
  `;
}

export function Toolbar() {
  const item = currentNavItem();
  const isChat = item.route === '#/chat';
  return html`
    <header id="toolbar" class="toolbar">
      <${SurfaceSwitcher} />
      <div class="toolbar-title">
        <h1>${isChat ? 'Echo' : item.label}</h1>
        ${isChat ? html`<${ConnStatus} />` : html`<${Subtitle} />`}
      </div>
      <div class="toolbar-trailing">
        <${PauseButton} />
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
