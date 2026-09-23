// The app shell's navigation: a bottom tab bar under 900px, a left
// sidebar at/above it (app.css's `.nav`/`.nav-item` media query does
// the actual layout switch). Array-driven per the plan, so a future
// screen only means adding a row here and to ui/Shell.js's SCREENS map
// -- W1 has exactly one.
import { html } from '../html.js';
import { route } from '../store.js';

const NAV_ITEMS = [{ route: '#/chat', label: 'Чат' }];

export function Nav() {
  return html`
    <nav id="nav" class="nav" aria-label="Разделы">
      ${NAV_ITEMS.map(
        (item) => html`
          <a
            key=${item.route}
            class="nav-item"
            href=${item.route}
            aria-current=${route.value === item.route ? 'page' : undefined}
          >
            <span class="nav-label">${item.label}</span>
          </a>
        `,
      )}
    </nav>
  `;
}
