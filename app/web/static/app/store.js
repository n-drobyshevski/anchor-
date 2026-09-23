// Shared reactive state, replacing the old app.js's scattered `let`s
// and direct DOM writes. Every signal here is read by one or more
// components (auto-subscribing on render, via signals.module.js's
// Preact integration) and written by api.js/sse.js/screens without
// either side needing to know who else is listening.
import { signal } from '../vendor/signals.module.js';

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

export function pushToast(text) {
  const id = nextToastId++;
  toasts.value = [...toasts.value, { id, text }];
  setTimeout(() => {
    toasts.value = toasts.value.filter((t) => t.id !== id);
  }, 3500);
}

// Bumped to a fresh {topic} object on every SSE "invalidate" event.
// No screen reads this in W1 -- it exists purely so sse.js can publish
// the event without knowing who (if anyone) eventually cares, per the
// contract's new "invalidate" event.
export const invalidate = signal(null);

// The bot-is-typing indicator; mirrors the old #typing-indicator's
// hidden flag. sse.js flips it on an SSE "typing" event and back off
// 6s later (or on the next message), same timeout as before.
export const typing = signal(false);
