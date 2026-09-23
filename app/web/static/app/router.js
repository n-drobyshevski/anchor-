// Hash router: one job, keep store.js's `route` signal in sync with
// `location.hash`, normalizing anything unrecognized back to the
// default screen. It does not pick a component itself -- ui/Shell.js
// reads `route` and maps it to a screen -- so adding a screen later
// only means adding its hash here and to ui/Nav.js's NAV_ITEMS.
import { route } from './store.js';

const KNOWN_ROUTES = ['#/chat'];
const DEFAULT_ROUTE = '#/chat';

function normalize(hash) {
  return KNOWN_ROUTES.includes(hash) ? hash : DEFAULT_ROUTE;
}

function sync() {
  const normalized = normalize(location.hash || DEFAULT_ROUTE);
  if (location.hash !== normalized) {
    // replaceState, not an assignment to location.hash: a stale or
    // unknown hash (an old bookmark, a future tab's link pasted back
    // into an unupgraded client) is corrected in place rather than
    // adding a back-button stop that points at itself. It does not
    // fire 'hashchange', so this does not recurse.
    history.replaceState(null, '', normalized);
  }
  route.value = normalized;
}

export function start() {
  window.addEventListener('hashchange', sync);
  sync();
}
