// htm-over-Preact template tag, plus the one DOM sink every future
// screen must route a dynamic URL through.
//
// `html` replaces JSX: `` html`<div class=${x}>${y}</div>` `` compiles
// the tagged template into `h(...)` calls at call time, with no build
// step and no `eval`/`new Function` anywhere in htm's own source
// (checked in step 1's vendoring). Preact then renders every child as
// text (`Text` nodes), never HTML, so nothing here can turn untrusted
// bot or user text into markup -- the same guarantee the old app.js's
// `textContent`-only rule gave, just via the framework instead of by
// hand.
import { h, render } from '../vendor/preact.module.js';
import htm from '../vendor/htm.module.js';

export const html = htm.bind(h);
export { render };

// Restricts a dynamically-built URL to the `https:` scheme, for any
// future `href`/`src` this app sets from data it does not fully
// control. Nothing in W1 renders such a link -- Chat.js never builds
// one -- but Trusted Types' `require-trusted-types-for 'script'` does
// not cover navigation sinks at all, so the very first screen that
// *does* link out must go through this rather than trust the string.
// Anything other than an absolute `https:` URL (a `javascript:`,
// `data:`, `#fragment`, or unparseable string) falls back to `'#'`, a
// dead but harmless link, instead of the unsafe value.
export function safeUrl(value) {
  if (typeof value === 'string') {
    try {
      const url = new URL(value, window.location.href);
      if (url.protocol === 'https:') return url.href;
    } catch {
      // fall through to the safe default below
    }
  }
  return '#';
}
