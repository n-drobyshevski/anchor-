// The authenticated app frame: Nav plus whichever screen `route`
// (store.js, kept in sync by router.js) points at.
//
// Chat is the one exception to "render whichever screen `route`
// points at": it is always mounted, `hidden` (Chat.js's own prop) on
// every route but '#/chat', instead of being swapped in and out of
// OTHER_SCREENS like every later screen. Unmounting/remounting it on
// each tab switch used to (a) throw away the composer draft and scroll
// position on every trip away from #/chat and back, and (b) reopen the
// exact SSE ordering race sse.js's history-load gate exists to close:
// the remount's fresh loadHistory() and a live event racing it could
// land in the wrong order, because nothing reset the gate on unmount
// (only a login/logout does). Keeping Chat mounted avoids both --
// State/Memory/Checkin/Proposals stay mount-per-visit, since none of them owns
// unrecoverable per-visit state the way Chat's log/composer do, and
// each already refetches on mount via hooks.js's useAutoRefetch.
import { html } from '../html.js';
import { Chat } from '../screens/Chat.js';
import { State } from '../screens/State.js';
import { Memory } from '../screens/Memory.js';
import { Checkin } from '../screens/Checkin.js';
import { Proposals } from '../screens/Proposals.js';
import { route } from '../store.js';
import { Nav } from './Nav.js';

// A later screen that should behave like State/Memory/Proposals (mount
// only while active) adds its hash here and to ui/Nav.js's NAV_ITEMS --
// nowhere else, since router.js already normalizes any hash outside
// KNOWN_ROUTES back to '#/chat'.
const OTHER_SCREENS = {
  '#/state': State,
  '#/memory': Memory,
  '#/checkin': Checkin,
  '#/proposals': Proposals,
};

export function Shell() {
  // Chat shows exactly when no other screen claims the route, so a new
  // entry in OTHER_SCREENS never needs a matching edit here.
  const OtherScreen = OTHER_SCREENS[route.value];
  const isChat = !OtherScreen;
  return html`
    <div id="shell" class="shell">
      <${Nav} />
      <div class="shell-screen">
        <${Chat} hidden=${!isChat} />
        ${OtherScreen ? html`<${OtherScreen} />` : null}
      </div>
    </div>
  `;
}
