// Shared reactive state, replacing the old app.js's scattered `let`s
// and direct DOM writes. Every signal here is read by one or more
// components (auto-subscribing on render, via signals.module.js's
// Preact integration) and written by api.js/sse.js/screens without
// either side needing to know who else is listening.
import { signal } from '../vendor/signals.module.js';
import { apiPost } from './api.js';

// The single source of truth for which screen the app shows:
//   'unknown' - before the bootstrap GET /api/me (main.js) resolves
//   'none'    - login screen, passphrase step
//   'code'    - login screen, code step (a passphrase already verified)
//   'in'      - authenticated: the chat shell
// Replaces the old showLogin()/showChat()/setLoginStage() trio; App.js
// picks a screen from this value alone.
export const auth = signal('unknown');

// Forces the login screen's passphrase step, exactly like the old
// goToLogin(): a session that died mid-use (a 401 from history/send/
// press, or the SSE error handler's own /api/me check) always restarts
// there, never at the code step. sse.js's effect (wired in main.js)
// reacts to `auth` leaving 'in' and tears the EventSource down.
export function forceLogout() {
  auth.value = 'none';
}

// The toolbar's «Выйти» (ui/Toolbar.js). Was Chat.js's handleLogout
// until the toolbar replaced #chat-header; unchanged in behavior.
export async function logout() {
  await apiPost('/api/auth/logout', {});
  forceLogout();
}

// The toolbar's «Пауза» (ui/Toolbar.js) sends `/out` exactly as it did
// from #chat-header: through Chat's own sendMessage, so it still shows
// as an optimistic own message and goes through POST /api/send. Chat
// is always mounted while logged in (ui/Shell.js), and registers its
// handler on mount; requestPause() before that is a no-op.
let pauseHandler = null;

export function registerPauseHandler(fn) {
  pauseHandler = fn;
  return () => {
    if (pauseHandler === fn) pauseHandler = null;
  };
}

export function requestPause() {
  if (pauseHandler) pauseHandler();
}

// An optional second line under the toolbar's screen title (for
// State: «следующее сообщение ~HH:MM»). The screen that sets it clears
// it again on unmount.
export const screenSubtitle = signal('');

// SSE connection indicator; mirrors the old #conn-dot dataset.state.
export const conn = signal('down');

// True once sseErrorCount has reached 3 with no successful reconnect
// since; mirrors the old #reconnect-banner's hidden flag (inverted).
export const reconnectBanner = signal(false);

// The current hash route (e.g. '#/chat'), kept for router.js and any
// screen that wants to know it. See router.js for how it is written.
export const route = signal(location.hash || '#/chat');

// Toast queue, oldest first: [{id, text}]. Toasts.js renders it and
// nothing else mutates it directly -- always go through pushToast().
export const toasts = signal([]);

let nextToastId = 1;

// At most TOAST_MAX on screen. The same text while it is still showing
// (five quick "Сохранено." in a row) restarts that toast's timer instead
// of stacking a copy of it.
const TOAST_MAX = 3;
const toastTimers = new Map();

export function pushToast(text) {
  const existing = toasts.value.find((t) => t.text === text);
  const id = existing ? existing.id : nextToastId++;
  if (existing) {
    clearTimeout(toastTimers.get(id));
  } else {
    toasts.value = [...toasts.value, { id, text }].slice(-TOAST_MAX);
  }
  toastTimers.set(
    id,
    setTimeout(() => {
      toastTimers.delete(id);
      toasts.value = toasts.value.filter((t) => t.id !== id);
    }, 3500),
  );
}

// Bumped to a fresh {topic} object on every SSE "invalidate" event.
// No screen read this in W1 -- it existed purely so sse.js could
// publish the event without knowing who (if anyone) eventually cared.
// W2's screens/State.js and screens/Proposals.js are the first
// readers, via hooks.js's useAutoRefetch().
export const invalidate = signal(null);

// 1 when a proposal is awaiting a decision, 0 otherwise -- the
// switcher's pip and the «Предложения» item's count
// (ui/SurfaceSwitcher.js). Populated from two places, both
// GET /api/proposals responses: main.js, right after login and on
// every SSE invalidate("proposals") *regardless of which screen is
// open* (the plan's "not only when the screen is open" rule -- a
// proposal raised while the user sits on #/chat still needs to show
// up here), and screens/Proposals.js's own reload while that screen is
// mounted, which would otherwise wait on a second, redundant
// round-trip from main.js's listener for the exact same event.
export const proposalsBadge = signal(0);

// The bot-is-typing indicator; mirrors the old #typing-indicator's
// hidden flag. sse.js flips it on an SSE "typing" event and back off
// 6s later (or on the next message), same timeout as before.
export const typing = signal(false);
