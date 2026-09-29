// The one list of screens: router.js accepts exactly these hashes,
// ui/SurfaceSwitcher.js lists them in this order, and ui/Shell.js maps
// each to its component (kept there, so this module imports no
// screens and no screen can import it back in a cycle). Adding a
// screen means one row here and one entry in Shell's SCREENS.
export const ROUTES = [
  { route: '#/chat', label: 'Чат', icon: 'message-circle' },
  { route: '#/state', label: 'Состояние', icon: 'gauge' },
  { route: '#/memory', label: 'Память', icon: 'bookmark' },
  { route: '#/checkin', label: 'Чек-ин', icon: 'circle-check' },
  { route: '#/proposals', label: 'Предложения', icon: 'inbox' },
];

export const DEFAULT_ROUTE = '#/chat';
