// Small screen-level data hook shared by every panel screen (State.js,
// Proposals.js, and whatever W3+ adds): fetch once on mount, then
// again whenever store.js's `invalidate` signal names a topic this
// screen displays, or the tab becomes visible again -- the three
// refetch triggers the roadmap's plan section 2 ("Live updates for
// panels") and the W2 task brief both call for. Not a generic
// data-fetching library: screens still own their own `useState` for
// the fetched value, loading and error handling: this only wires the
// two extra triggers so each screen does not repeat the same wiring.
import { useEffect } from '../vendor/hooks.module.js';
import { invalidate } from './store.js';

// `topic` is a single topic string, or an array of them -- State.js
// needs several: app/web/tail.py's STATE_CHANGE_FIELD_TOPIC maps
// streak/last_checkin_at to 'checkin' and memory-count changes to
// 'memory', not 'state', so a screen showing those fields must refetch
// on those topics too, or it goes stale after a Telegram check-in or a
// /forget until the next tab-visibility change. `'*'` (main.js's SSE-
// reconnect resync) always matches, the same way a lost invalidate
// during an outage is recovered for the proposals badge.
export function useAutoRefetch(topic, reload) {
  const topics = Array.isArray(topic) ? topic : [topic];

  // Read during render (not inside an effect) so this component
  // itself subscribes to `invalidate`, the same way screens/Chat.js's
  // ConnDot subscribes to `conn` -- ports Signals' own auto-subscribe
  // mechanism, which only tracks `.value` reads made while rendering.
  const current = invalidate.value;

  useEffect(() => {
    reload();
    // Intentionally once per mount only -- `reload` is a fresh closure
    // every render (it captures the screen's own setState calls), so
    // including it here would refetch on every render instead of only
    // the triggers this hook exists for.
    // eslint-disable-next-line
  }, []);

  useEffect(() => {
    if (current && (current.topic === '*' || topics.includes(current.topic))) reload();
    // eslint-disable-next-line
  }, [current]);

  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState === 'visible') reload();
    }
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
    // eslint-disable-next-line
  }, []);
}
