// Hash router: one job, keep store.js's `route` signal in sync with
// `location.hash`, normalizing anything unrecognized back to the
// default screen. It does not pick a component itself -- ui/Shell.js
// reads `route` and maps it to a screen -- so adding a screen later
// only means adding it to routes.js (and its component to Shell.js).
import { route, routeQuery } from './store.js';
import { DEFAULT_ROUTE, LEGACY_ROUTES, ROUTES } from './routes.js';

const KNOWN_ROUTES = ROUTES.map((r) => r.route);

function normalize(path) {
  if (LEGACY_ROUTES[path]) return LEGACY_ROUTES[path];
  return KNOWN_ROUTES.includes(path) ? path : DEFAULT_ROUTE;
}

// A hash is a route plus an optional query a screen owns
// (`#/memory?tab=notebook` keeps Память's tab across a reload). The
// query survives only on the route it was written for.
function sync() {
  const raw = location.hash || DEFAULT_ROUTE;
  const q = raw.indexOf('?');
  const path = q === -1 ? raw : raw.slice(0, q);
  const query = q === -1 ? '' : raw.slice(q + 1);
  const route_ = normalize(path);
  const normalized = route_ === path && query ? `${route_}?${query}` : route_;
  if (location.hash !== normalized) {
    // replaceState, not an assignment to location.hash: a stale or
    // unknown hash (an old bookmark, a future tab's link pasted back
    // into an unupgraded client) is corrected in place rather than
    // adding a back-button stop that points at itself. It does not
    // fire 'hashchange', so this does not recurse.
    history.replaceState(null, '', normalized);
  }
  routeQuery.value = route_ === path ? query : '';
  route.value = route_;
}

// A screen's own state in the hash (Память's tab): replaces the query
// in place, no history entry, no hashchange.
export function setRouteQuery(query) {
  history.replaceState(null, '', query ? `${route.value}?${query}` : route.value);
  routeQuery.value = query;
}

export function start() {
  window.addEventListener('hashchange', sync);
  sync();
}
