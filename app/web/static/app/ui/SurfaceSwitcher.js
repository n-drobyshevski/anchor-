// The section switcher at the left of the toolbar (ui/Toolbar.js), after
// Planner's AppNav: one outline button showing the current screen's
// icon, opening a small menu of every screen. It replaces the old
// bottom tab bar / left sidebar (ui/Nav.js), and works the same at
// every width.
//
// Array-driven: a future screen means one row in NAV_ITEMS plus its
// entry in ui/Shell.js's OTHER_SCREENS and router.js's KNOWN_ROUTES.
//
// Keyboard: the button opens the menu and focuses the current item;
// Up/Down move between items (wrapping), Home/End jump to the ends,
// Escape closes and returns focus to the button. A click outside, or
// choosing an item, closes it too. Items are plain <a href="#/...">
// links, so router.js's hashchange handling does the navigating. No
// focus trap: Tab simply leaves the menu, and the menu closes as soon
// as focus is outside it.
import { html } from '../html.js';
import { useEffect, useRef, useState } from '../../vendor/hooks.module.js';
import { proposalsBadge, route } from '../store.js';
import { Icon } from './Icon.js';

export const NAV_ITEMS = [
  { route: '#/chat', label: 'Чат', icon: 'message-circle' },
  { route: '#/state', label: 'Состояние', icon: 'gauge' },
  { route: '#/memory', label: 'Память', icon: 'bookmark' },
  { route: '#/checkin', label: 'Чек-ин', icon: 'circle-check' },
  { route: '#/proposals', label: 'Предложения', icon: 'inbox' },
];

const MENU_ID = 'surface-menu';

export function currentNavItem() {
  return NAV_ITEMS.find((item) => item.route === route.value) || NAV_ITEMS[0];
}

export function SurfaceSwitcher() {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const buttonRef = useRef(null);
  const menuRef = useRef(null);

  const current = currentNavItem();
  const badge = proposalsBadge.value;
  // The pip only nags while you are somewhere else: on #/proposals
  // itself the pending card is already in front of you.
  const showPip = badge > 0 && route.value !== '#/proposals';
  const buttonLabel = showPip
    ? `Раздел: ${current.label}. Предложений: ${badge}`
    : `Раздел: ${current.label}`;

  function items() {
    return menuRef.current ? Array.from(menuRef.current.querySelectorAll('a')) : [];
  }

  function close({ refocus }) {
    setOpen(false);
    if (refocus && buttonRef.current) buttonRef.current.focus();
  }

  // Opening focuses the current item (after the menu has rendered).
  useEffect(() => {
    if (!open) return;
    const list = items();
    const active = list.find((a) => a.getAttribute('aria-current') === 'page') || list[0];
    if (active) active.focus();
  }, [open]);

  // Click/tap outside closes; so does focus leaving the switcher
  // entirely (Tab past the last item).
  useEffect(() => {
    if (!open) return undefined;
    function onPointerDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) close({ refocus: false });
    }
    function onFocusIn(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) close({ refocus: false });
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [open]);

  // Any route change (an item chosen here, or the browser's back
  // button) closes the menu.
  useEffect(() => {
    setOpen(false);
  }, [route.value]);

  function onMenuKeyDown(e) {
    const list = items();
    const i = list.indexOf(document.activeElement);
    let next = null;
    if (e.key === 'ArrowDown') next = list[(i + 1) % list.length];
    else if (e.key === 'ArrowUp') next = list[(i - 1 + list.length) % list.length];
    else if (e.key === 'Home') next = list[0];
    else if (e.key === 'End') next = list[list.length - 1];
    else if (e.key === 'Escape') {
      e.preventDefault();
      close({ refocus: true });
      return;
    }
    if (next) {
      e.preventDefault();
      next.focus();
    }
  }

  function onButtonKeyDown(e) {
    if (e.key === 'ArrowDown' && !open) {
      e.preventDefault();
      setOpen(true);
    } else if (e.key === 'Escape' && open) {
      e.preventDefault();
      close({ refocus: true });
    }
  }

  function onItemClick(item) {
    // Choosing the screen you are already on changes no hash, so the
    // route effect above would not fire: close explicitly.
    close({ refocus: item.route === route.value });
  }

  return html`
    <div class="switcher" ref=${rootRef}>
      <button
        type="button"
        id="surface-button"
        class="switcher-button"
        ref=${buttonRef}
        aria-label=${buttonLabel}
        aria-expanded=${open ? 'true' : 'false'}
        aria-controls=${MENU_ID}
        onClick=${() => (open ? close({ refocus: false }) : setOpen(true))}
        onKeyDown=${onButtonKeyDown}
      >
        <span class="switcher-tile">
          <${Icon} name=${current.icon} size=${16} />
          ${showPip ? html`<span class="switcher-pip"></span>` : null}
        </span>
        <${Icon} name="chevron-down" size=${16} class="switcher-chevron" />
      </button>
      <nav
        id=${MENU_ID}
        class="switcher-menu"
        aria-label="Разделы"
        ref=${menuRef}
        hidden=${!open}
        onKeyDown=${onMenuKeyDown}
      >
        ${NAV_ITEMS.map((item) => {
          const isCurrent = route.value === item.route;
          // Only #/proposals carries a count today (store.js's
          // proposalsBadge). A later screen that wants one reads its
          // own signal here the same way.
          const count = item.route === '#/proposals' ? badge : 0;
          return html`
            <a
              key=${item.route}
              class="switcher-item"
              href=${item.route}
              aria-current=${isCurrent ? 'page' : undefined}
              aria-label=${count ? `${item.label}, ${count}` : undefined}
              onClick=${() => onItemClick(item)}
            >
              <${Icon} name=${item.icon} size=${16} />
              <span class="switcher-item-label">${item.label}</span>
              ${count ? html`<span class="nav-badge">${count}</span>` : null}
              ${isCurrent ? html`<${Icon} name="check" size=${16} class="switcher-check" />` : null}
            </a>
          `;
        })}
      </nav>
    </div>
  `;
}
