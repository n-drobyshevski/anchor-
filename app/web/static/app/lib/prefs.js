// Appearance preferences (Настройки → Оформление): theme and density,
// kept in this browser's localStorage, never on the server. boot.js
// applies the stored values before first paint; this module reads and
// changes them afterwards. Every storage access is wrapped: a private
// window or blocked site data throws, and the page must still work
// (it then follows the system theme at normal density).
const THEME_KEY = 'echo.theme';
const DENSITY_KEY = 'echo.density';

export const THEMES = ['system', 'light', 'dark'];
export const DENSITIES = ['comfortable', 'compact'];

// The browser chrome colour per theme: app.css's --bg.
const THEME_COLOR = { light: '#f4f4f1', dark: '#161917' };

function read(key, allowed, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value) ? value : fallback;
  } catch (e) {
    return fallback;
  }
}

function write(key, value, fallback) {
  try {
    if (value === fallback) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch (e) {
    // Not saved: the choice still applies to this page until reload.
  }
}

export function getPrefs() {
  return {
    theme: read(THEME_KEY, THEMES, 'system'),
    density: read(DENSITY_KEY, DENSITIES, 'comfortable'),
  };
}

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  // index.html's two theme-color metas are keyed on the system scheme;
  // a manual theme gives both the chosen colour, 'system' restores them.
  for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
    const scheme = (meta.getAttribute('media') || '').includes('dark') ? 'dark' : 'light';
    meta.setAttribute('content', THEME_COLOR[theme === 'system' ? scheme : theme]);
  }
}

function applyDensity(density) {
  const root = document.documentElement;
  if (density === 'compact') root.setAttribute('data-density', 'compact');
  else root.removeAttribute('data-density');
}

export function setTheme(theme) {
  if (!THEMES.includes(theme)) return;
  write(THEME_KEY, theme, 'system');
  applyTheme(theme);
}

export function setDensity(density) {
  if (!DENSITIES.includes(density)) return;
  write(DENSITY_KEY, density, 'comfortable');
  applyDensity(density);
}
