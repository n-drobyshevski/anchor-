// Appearance, applied before first paint. A classic (not module)
// script, loaded blocking from index.html's <head>, so a dark-by-choice
// page never flashes light. It only sets two attributes on <html>;
// lib/prefs.js owns the same keys and changes them later from
// Настройки → Оформление. Stored in this browser only (localStorage),
// and every access is guarded: private windows and blocked storage
// throw, and then the page simply follows the system.
(function () {
  var root = document.documentElement;
  var theme = null;
  var density = null;
  try {
    theme = window.localStorage.getItem('echo.theme');
    density = window.localStorage.getItem('echo.density');
  } catch (e) {
    return;
  }
  if (theme === 'light' || theme === 'dark') {
    root.setAttribute('data-theme', theme);
    var metas = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < metas.length; i += 1) {
      metas[i].setAttribute('content', theme === 'dark' ? '#161917' : '#f4f4f1');
    }
  }
  if (density === 'compact') root.setAttribute('data-density', 'compact');
})();
