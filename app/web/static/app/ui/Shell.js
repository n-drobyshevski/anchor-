// The authenticated app frame: Nav plus whichever screen `route`
// (store.js, kept in sync by router.js) points at.
import { html } from '../html.js';
import { Chat } from '../screens/Chat.js';
import { route } from '../store.js';
import { Nav } from './Nav.js';

// One entry today. A later screen adds its hash here and to
// ui/Nav.js's NAV_ITEMS -- nowhere else, since router.js already
// normalizes any hash outside KNOWN_ROUTES back to '#/chat'.
const SCREENS = {
  '#/chat': Chat,
};

export function Shell() {
  const Screen = SCREENS[route.value] || Chat;
  return html`
    <div id="shell" class="shell">
      <${Nav} />
      <div class="shell-screen">
        <${Screen} />
      </div>
    </div>
  `;
}
