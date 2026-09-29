// Screen-level data hooks shared by every panel screen.
//
// useAutoRefetch wires the three refetch triggers the roadmap's plan
// section 2 ("Live updates for panels") calls for: mount, an SSE
// `invalidate` naming a topic the screen displays, and the tab becoming
// visible again. useResource and usePagedResource build the fetched
// value, loading and error handling on top of it, so a screen no
// longer repeats that plumbing (or its stale-response guards).
import { useEffect, useLayoutEffect, useRef, useState } from '../vendor/hooks.module.js';
import { invalidate, pushToast } from './store.js';
import { load } from './lib/request.js';

// A tab coming back to the foreground refetches at most this often:
// the SSE stream keeps delivering invalidates while a tab is in the
// background, so this is only the recovery path for a missed one, not
// the primary way data stays fresh.
const VISIBILITY_MIN_INTERVAL_MS = 10000;

// `topic` is a single topic string, or an array of them -- a screen
// showing fields app/web/tail.py maps to several topics (State's
// streak is 'checkin', its memory count is 'memory') lists them all.
// `'*'` (sse.js's SSE-reconnect resync) always matches.
export function useAutoRefetch(topic, reload) {
  // Both kept in refs, updated every render, so the listeners below
  // (registered once) call *today's* reload with today's topics -- a
  // screen with filters must not refetch the unfiltered first page
  // (W3 finding).
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const topicsRef = useRef(topic);
  topicsRef.current = Array.isArray(topic) ? topic : [topic];
  const lastRunRef = useRef(0);

  function run() {
    lastRunRef.current = Date.now();
    reloadRef.current();
  }

  useEffect(() => {
    run();
    // Subscribed in an effect, not read during render: reading
    // `invalidate.value` while rendering subscribed the whole screen,
    // re-rendering it on every invalidate, even for topics it does not
    // show. `subscribe` calls back once immediately with whatever
    // invalidate was last published, possibly long before this screen
    // mounted -- skipped, since the mount fetch above already covers it
    // (it used to cause a second, identical fetch on mount).
    let first = true;
    const unsubscribe = invalidate.subscribe((current) => {
      if (first) {
        first = false;
        return;
      }
      if (current && (current.topic === '*' || topicsRef.current.includes(current.topic))) run();
    });
    function onVisibilityChange() {
      if (document.visibilityState !== 'visible') return;
      if (Date.now() - lastRunRef.current < VISIBILITY_MIN_INTERVAL_MS) return;
      run();
    }
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      unsubscribe();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
    // eslint-disable-next-line
  }, []);
}

// False once the component has unmounted: a response that lands after
// that is dropped instead of calling setState on a dead component.
export function useMountedRef() {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return mounted;
}

// One GET endpoint, kept fresh by useAutoRefetch. Returns
//   data    - the last good response body (null until the first lands)
//   failed  - true when the last load failed
//   reload  - fetch again now
//   replace - install a value the caller already has (a mutation's
//             response); it also orphans any GET still in flight, so a
//             read that started before the write cannot land after it
//             and put the old value back.
// `accept(data)` can reject a malformed 2xx body.
export function useResource(path, topics, { accept = () => true } = {}) {
  const [state, setState] = useState({ data: null, failed: false });
  const seqRef = useRef(0);
  const mounted = useMountedRef();

  async function reload() {
    const seq = ++seqRef.current;
    const res = await load(path);
    if (!mounted.current || seq !== seqRef.current || res.status === 401) return;
    if (res.ok && accept(res.data)) setState({ data: res.data, failed: false });
    else setState((s) => ({ data: s.data, failed: true }));
  }

  function replace(data) {
    seqRef.current += 1;
    setState({ data, failed: false });
  }

  useAutoRefetch(topics, reload);
  return { data: state.data, failed: state.failed, reload, replace };
}

// A paged list endpoint ({items, total, ...} with offset/limit), kept
// fresh by useAutoRefetch. `query(offset, limit)` builds the URL; `key`
// names the current filter, and changing it starts over from the first
// page. A refresh (mount, invalidate, tab focus) refetches however many
// rows are already loaded, in chunks of `maxLimit`, so it never undoes
// a «Показать ещё» (W3 finding); «Показать ещё» appends, deduped by id
// since a row written between pages shifts offsets.
//
// Returns {items, total, meta, loaded, failed, loadingMore, switching,
// reload, loadMore, patch}: `meta` is the last page's whole body (for
// extra fields like Memory's pinned counts), `switching` is true while
// a filter change is loading, and `patch(fn)` applies a local change
// to {items, total, meta} after a successful write.
export function usePagedResource(query, topics, { key = '', pageSize = 30, maxLimit = 50 } = {}) {
  const [state, setState] = useState({ items: [], total: 0, meta: null, loaded: false, failed: false });
  const [loadingMore, setLoadingMore] = useState(false);
  const [switching, setSwitching] = useState(false);
  const mounted = useMountedRef();
  // Bumped by every replacing fetch; a response applies only if this
  // has not moved on since, and an append also only if the filter has
  // not changed under it.
  const seqRef = useRef(0);
  const keyRef = useRef(key);
  keyRef.current = key;
  const queryRef = useRef(query);
  queryRef.current = query;
  const itemsRef = useRef(state.items);
  itemsRef.current = state.items;

  function isValid(res) {
    return res.ok && Array.isArray(res.data.items);
  }

  async function reload() {
    const seq = ++seqRef.current;
    const target = Math.max(itemsRef.current.length, pageSize);
    const collected = [];
    let last = null;
    for (let offset = 0; offset < target; offset += maxLimit) {
      const limit = Math.min(maxLimit, target - offset);
      const res = await load(queryRef.current(offset, limit));
      if (!mounted.current || seq !== seqRef.current || res.status === 401) return;
      if (!isValid(res)) {
        setState((s) => ({ ...s, failed: true }));
        return;
      }
      collected.push(...res.data.items);
      last = res.data;
      if (res.data.items.length < limit) break; // fewer rows left than this chunk covers
    }
    setState({ items: collected, total: last.total, meta: last, loaded: true, failed: false });
  }

  async function loadMore() {
    const seq = seqRef.current;
    const keyAtCall = keyRef.current;
    setLoadingMore(true);
    const res = await load(queryRef.current(itemsRef.current.length, pageSize));
    if (!mounted.current) return;
    setLoadingMore(false);
    if (res.status === 401 || seq !== seqRef.current || keyAtCall !== keyRef.current) return;
    if (!isValid(res)) {
      pushToast('Не удалось загрузить.');
      return;
    }
    setState((s) => {
      const seen = new Set(s.items.map((it) => it.id));
      return {
        ...s,
        items: [...s.items, ...res.data.items.filter((it) => !seen.has(it.id))],
        total: res.data.total,
        meta: res.data,
      };
    });
  }

  function patch(fn) {
    setState((s) => ({ ...s, ...fn(s) }));
  }

  useAutoRefetch(topics, reload);

  // A filter change is a fresh query, not a refresh: back to the first
  // page. Skipped on mount, where useAutoRefetch already fetches.
  const firstKeyRef = useRef(true);
  useEffect(() => {
    if (firstKeyRef.current) {
      firstKeyRef.current = false;
      return;
    }
    itemsRef.current = [];
    setState((s) => ({ ...s, items: [], failed: false }));
    setSwitching(true);
    reload().finally(() => {
      if (mounted.current) setSwitching(false);
    });
    // eslint-disable-next-line
  }, [key]);

  return { ...state, loadingMore, switching, reload, loadMore, patch };
}

// An element's width in px, tracked with ResizeObserver: [ref, width].
export function useElementWidth(fallback) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => {
      const w = Math.floor(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}
