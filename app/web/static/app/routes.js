// The one list of screens: router.js accepts exactly these hashes,
// ui/SurfaceSwitcher.js lists them in this order, and ui/Shell.js maps
// each to its component (kept there, so this module imports no
// screens and no screen can import it back in a cycle). Adding a
// screen means one row here and one entry in Shell's SCREENS.
export const ROUTES = [
  { route: '#/today', label: 'Сегодня', icon: 'sun' },
  { route: '#/chat', label: 'Чат', icon: 'message-circle' },
  { route: '#/memory', label: 'Память', icon: 'bookmark' },
  { route: '#/journal', label: 'Дневник', icon: 'book-open' },
  { route: '#/usage', label: 'Лимиты', icon: 'gauge' },
  { route: '#/settings', label: 'Настройки', icon: 'settings' },
];

export const DEFAULT_ROUTE = '#/chat';

// Where the screens that were merged away now live, so an old bookmark
// or link lands on the page that holds what it pointed at.
export const LEGACY_ROUTES = {
  '#/state': '#/today',
  '#/proposals': '#/today',
  '#/checkin': '#/today',
};
