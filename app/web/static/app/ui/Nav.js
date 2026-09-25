// The app shell's navigation: a bottom tab bar under 900px, a left
// sidebar at/above it (app.css's `.nav`/`.nav-item` media query does
// the actual layout switch). Array-driven per the plan, so a future
// screen only means adding a row here and to ui/Shell.js's SCREENS
// map. Per the roadmap's mobile-nav rule (5 items max) this is now
// full: Чат · Состояние · Память · Чек-ин · Предложения (W4). app.css
// narrows the bottom-bar items and lifts the badge out of the flow so
// all five fit a 390px-wide phone.
import { html } from '../html.js';
import { proposalsBadge, route } from '../store.js';

const NAV_ITEMS = [
  { route: '#/chat', label: 'Чат' },
  { route: '#/state', label: 'Состояние' },
  { route: '#/memory', label: 'Память' },
  { route: '#/checkin', label: 'Чек-ин' },
  { route: '#/proposals', label: 'Предложения' },
];

export function Nav() {
  return html`
    <nav id="nav" class="nav" aria-label="Разделы">
      ${NAV_ITEMS.map((item) => {
        // Only #/proposals carries a badge today (store.js's
        // proposalsBadge). A later screen that wants one only needs to
        // read its own signal here the same way -- nothing else about
        // this loop is proposals-specific.
        const badge = item.route === '#/proposals' ? proposalsBadge.value : 0;
        return html`
          <a
            key=${item.route}
            class="nav-item"
            href=${item.route}
            aria-current=${route.value === item.route ? 'page' : undefined}
            aria-label=${badge ? `${item.label}, ${badge}` : undefined}
          >
            <span class="nav-label">${item.label}</span>
            ${badge ? html`<span class="nav-badge">${badge}</span>` : null}
          </a>
        `;
      })}
    </nav>
  `;
}
