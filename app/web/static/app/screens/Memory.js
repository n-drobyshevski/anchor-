// The memory screen (#/memory, nav label "Память"): the W3 HTTP
// contract's memories list (GET /api/memories), plus pin/unpin,
// "Исправить" (an inline edit that supersedes the row, exactly like
// Telegram's correction path) and "Забыть" (hard delete, behind a
// native <dialog> confirmation) and an add form. One set of rules for
// Telegram and the web, per the plan: every mutation here calls the
// same app/core/memory.py functions Telegram calls, and is silent in
// Telegram (audit source "web", no send).
//
// Unlike State.js/Proposals.js, this screen paginates
// (offset/limit, "Показать ещё") -- see reload()'s own comment for how
// that interacts with the tail's invalidate-triggered refetch.
import { html } from '../html.js';
import { useEffect, useRef, useState } from '../../vendor/hooks.module.js';
import { apiGet, apiPost } from '../api.js';
import { forceLogout, pushToast } from '../store.js';
import { useAutoRefetch } from '../hooks.js';
import { Icon } from '../ui/Icon.js';
import { Toasts } from '../ui/Toasts.js';

// Mirrors app/core/memory.py's MEMORY_TEXT_MAX. Unlike State.js's
// `state.limits.due_max_len`, the memories contract carries no
// `limits` field for this screen to read the cap from, so this is a
// plain constant kept in sync with the backend by hand.
const MEMORY_TEXT_MAX = 300;

// The filter chips and the add-form <select> have no DTO to read a
// label from (unlike a listed item, which always carries its own
// `kind_label` from the backend -- see kindLabel() below, which
// prefers that). Mirrors app/core/memory.py's own KIND_LABEL, which
// covers all five KINDS (unlike app/tg/memory.py's KIND_LABELS, which
// deliberately omits `technique`: an adopt/extractor-only kind never
// offered to a Telegram user by hand, per that module's own comment --
// W3's web list shows every active memory regardless of source, so it
// needs a label for that kind too).
const KIND_LABELS = {
  identity: 'Обо мне',
  preference: 'Предпочтение',
  event: 'Событие',
  rule: 'Правило',
  technique: 'Техника',
};
// app/core/memory.py's KINDS order, reused for both the filter chips
// and the add-form select.
const KIND_ORDER = ['identity', 'preference', 'event', 'rule', 'technique'];

// MemoryDTO.source -> how each card credits where a memory came from.
// `consolidate` (idle merge, app/core/idle/consolidate.py) is the one
// KIND_LABEL-adjacent value this map used to miss -- source falls back
// to `|| item.source` in kindLabel()-style code below, which would
// otherwise show the raw English word in an all-Russian UI (W3
// finding).
const SOURCE_LABELS = {
  user: 'от тебя',
  extractor: 'Anchor запомнил',
  adopt: 'техника',
  consolidate: 'Anchor объединил',
};

const DETAIL_MESSAGES = {
  empty: 'Пусто',
  too_long: 'Слишком длинно',
  bad_kind: 'Неверный вид',
};

const PAGE_SIZE = 30;
// The endpoint's own cap ("limit(<=50)" in the contract) -- reload()
// below cannot ask for more than this in one request no matter how
// many rows are already loaded.
const MAX_LIMIT = 50;

function minutesText(retryAfterSeconds) {
  const m = Math.max(1, Math.ceil((retryAfterSeconds || 60) / 60));
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

function formatDateTime(iso) {
  return new Date(iso).toLocaleString('ru-RU');
}

function kindLabel(item) {
  // The backend's own kind_label always wins for a real item -- this
  // only falls back to the local map if a future kind this build does
  // not know about ever reaches the DTO.
  return item.kind_label || KIND_LABELS[item.kind] || item.kind;
}

// A small live region so a screen reader announces the count as it
// changes, notably as either counter crosses its limit (the pinned
// cap here, the 300-char cap in AddForm/inline edit below).
function CharCounter({ length, max }) {
  const over = length > max;
  return html`
    <p class="char-counter${over ? ' char-counter-over' : ''}" aria-live="polite">${length}/${max}</p>
  `;
}

function MemoryCard({ item, forgetBusy, onPin, onEdit, onForgetClick }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [error, setError] = useState('');
  const [duplicate, setDuplicate] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pinBusy, setPinBusy] = useState(false);
  const textareaRef = useRef(null);
  const editButtonRef = useRef(null);
  const forgetButtonRef = useRef(null);
  const wasEditingRef = useRef(false);

  useEffect(() => {
    if (editing && textareaRef.current) textareaRef.current.focus();
    // Same focus-return pattern as State.js's DueCard: editing just
    // closed (Escape, Сохранить, or a successful save), so hand focus
    // back to the button that opened it rather than letting it fall to
    // <body> when the textarea/buttons unmount.
    if (!editing && wasEditingRef.current && editButtonRef.current) editButtonRef.current.focus();
    wasEditingRef.current = editing;
  }, [editing]);

  function startEdit() {
    setDraft(item.text);
    setError('');
    setDuplicate(null);
    setEditing(true);
  }

  function cancelEdit() {
    setDraft(item.text);
    setError('');
    setDuplicate(null);
    setEditing(false);
  }

  async function save() {
    setBusy(true);
    setError('');
    setDuplicate(null);
    const result = await onEdit(item.id, draft);
    setBusy(false);
    if (result === true) {
      setEditing(false);
      return;
    }
    if (result && typeof result === 'object') {
      setDuplicate(result.duplicate);
      return;
    }
    setError(result);
  }

  function onKeyDown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancelEdit();
      return;
    }
    // Plain Enter inserts a newline; Ctrl/Cmd+Enter saves, same
    // convention as State.js's DueCard.
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!busy) save();
    }
  }

  async function togglePin() {
    setPinBusy(true);
    await onPin(item);
    setPinBusy(false);
  }

  const canSave = draft.trim().length > 0 && draft.length <= MEMORY_TEXT_MAX;
  const usedText = item.use_count > 0
    ? html`использовано <span class="mono">${item.use_count}</span> раз${item.last_used_at
        ? html` · последний раз <span class="mono">${formatDateTime(item.last_used_at)}</span>`
        : ''}`
    : 'ещё не использовалось';

  return html`
    <li class="memory-card card">
      <div class="card-row">
        <span class="kind-chip">${kindLabel(item)}</span>
        <button
          type="button"
          class="pin-toggle"
          aria-pressed=${item.pinned ? 'true' : 'false'}
          aria-label=${item.pinned ? 'Открепить' : 'Закрепить'}
          disabled=${pinBusy}
          onClick=${togglePin}
        >
          <${Icon} name="pin" size=${16} />
        </button>
      </div>
      ${editing
        ? html`
            <textarea
              ref=${textareaRef}
              class="field-edit"
              rows="3"
              aria-label="Текст воспоминания"
              disabled=${busy}
              value=${draft}
              onInput=${(e) => setDraft(e.target.value)}
              onKeyDown=${onKeyDown}
            ></textarea>
            <${CharCounter} length=${draft.length} max=${MEMORY_TEXT_MAX} />
            ${duplicate
              ? html`<p class="field-hint">Похожее уже есть: «${duplicate.text}»</p>`
              : null}
            ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
            <div class="btn-row">
              <button type="button" class="btn btn-primary" disabled=${busy || !canSave} onClick=${save}>
                Сохранить
              </button>
              <button type="button" class="btn btn-ghost" disabled=${busy} onClick=${cancelEdit}>Отмена</button>
            </div>
          `
        : html`
            <p class="field-value">${item.text}</p>
            <p class="field-hint">${SOURCE_LABELS[item.source] || item.source} · ${usedText}</p>
            <div class="btn-row">
              <button
                type="button"
                ref=${editButtonRef}
                data-memory-id=${item.id}
                class="btn btn-ghost"
                onClick=${startEdit}
              >
                Исправить
              </button>
              <button
                type="button"
                ref=${forgetButtonRef}
                class="btn btn-ghost btn-danger"
                disabled=${forgetBusy}
                onClick=${() => onForgetClick(item, forgetButtonRef)}
              >
                Забыть
              </button>
            </div>
          `}
    </li>
  `;
}

function AddForm({ open, onClose, onAdd }) {
  const [kind, setKind] = useState(KIND_ORDER[0]);
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [duplicate, setDuplicate] = useState(null);
  const [busy, setBusy] = useState(false);
  const textareaRef = useRef(null);

  useEffect(() => {
    if (open && textareaRef.current) textareaRef.current.focus();
  }, [open]);

  function reset() {
    setKind(KIND_ORDER[0]);
    setText('');
    setError('');
    setDuplicate(null);
  }

  async function submit() {
    setBusy(true);
    setError('');
    setDuplicate(null);
    const result = await onAdd(kind, text);
    setBusy(false);
    if (result === true) {
      reset();
      onClose();
      return;
    }
    if (result && typeof result === 'object') {
      setDuplicate(result.duplicate);
      return;
    }
    setError(result);
  }

  function cancel() {
    reset();
    onClose();
  }

  if (!open) return null;

  const canSave = text.trim().length > 0 && text.length <= MEMORY_TEXT_MAX;

  return html`
    <section class="card add-form" aria-labelledby="add-memory-heading">
      <h2 id="add-memory-heading">Новая запись</h2>
      <label for="add-memory-kind" class="field-hint">Вид</label>
      <select id="add-memory-kind" disabled=${busy} value=${kind} onChange=${(e) => setKind(e.target.value)}>
        ${KIND_ORDER.map((k) => html`<option key=${k} value=${k}>${KIND_LABELS[k]}</option>`)}
      </select>
      <label for="add-memory-text" class="field-hint">Текст</label>
      <textarea
        id="add-memory-text"
        ref=${textareaRef}
        class="field-edit"
        rows="3"
        disabled=${busy}
        value=${text}
        onInput=${(e) => setText(e.target.value)}
      ></textarea>
      <${CharCounter} length=${text.length} max=${MEMORY_TEXT_MAX} />
      ${duplicate ? html`<p class="field-hint">Похожее уже есть: «${duplicate.text}»</p>` : null}
      ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
      <div class="btn-row">
        <button type="button" class="btn btn-primary" disabled=${busy || !canSave} onClick=${submit}>
          Сохранить
        </button>
        <button type="button" class="btn btn-ghost" disabled=${busy} onClick=${cancel}>Отмена</button>
      </div>
    </section>
  `;
}

// A single confirmation <dialog>, reused for whichever card's "Забыть"
// was clicked -- `target` (below) holds {item, trigger}. Kept as one
// instance at the screen level rather than one per card, so there is
// never more than one modal in the tree.
function ForgetDialog({ target, busy, onCancel, onConfirm }) {
  const dialogRef = useRef(null);
  const cancelButtonRef = useRef(null);
  // Remembers the most recent non-null `target` even after the prop
  // itself goes back to null -- by the time the native 'close' event
  // below runs, the parent's own state (and this render's `target`
  // prop) may already have moved on, so this is what onNativeClose
  // reads to know which button to return focus to.
  const lastTargetRef = useRef(null);

  useEffect(() => {
    if (target) lastTargetRef.current = target;
  }, [target]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (target && !dialog.open) {
      dialog.showModal();
      if (cancelButtonRef.current) cancelButtonRef.current.focus();
    }
  }, [target]);

  function requestClose() {
    if (dialogRef.current && dialogRef.current.open) dialogRef.current.close();
  }

  function onNativeClose() {
    // Fires for every way the dialog closes: requestClose() below
    // (Отмена, and Забыть once onConfirm resolves) and the native
    // Escape path -- the UA fires 'cancel' then 'close' on its own for
    // Escape, so no separate keydown handler is needed to cover it.
    // Either way this is the one place that clears the pending target
    // (onCancel is idempotent -- a no-op if it is already null) and
    // returns focus to whatever button opened it. On a confirmed
    // Забыть the card (and its button) is already gone from the DOM by
    // now -- `isConnected` catches that (W3 finding: an unconnected
    // node's own `.focus()` is a silent no-op, so focus fell all the
    // way to <body> with nothing announced to a keyboard/AT user), and
    // falls back to the "+ Добавить" toggle, which always exists.
    // Отмена is the path where `trigger` is still connected and gets
    // focus back exactly as before.
    const trigger = lastTargetRef.current && lastTargetRef.current.trigger;
    onCancel();
    if (trigger && typeof trigger.focus === 'function' && trigger.isConnected) {
      trigger.focus();
    } else {
      const fallback = document.getElementById('memory-add-toggle');
      if (fallback) fallback.focus();
    }
  }

  async function handleConfirm() {
    await onConfirm();
    requestClose();
  }

  return html`
    <dialog ref=${dialogRef} class="confirm-dialog" aria-labelledby="forget-heading" onClose=${onNativeClose}>
      ${target
        ? html`
            <h2 id="forget-heading">Забыть навсегда?</h2>
            <p class="field-hint">«${target.item.text}»</p>
            ${target.item.has_predecessor
              ? html`<p role="alert">У этой записи есть более ранние версии — они забудутся вместе с ней.</p>`
              : null}
            <p>Это нельзя отменить.</p>
            <div class="btn-row">
              <button
                type="button"
                ref=${cancelButtonRef}
                class="btn btn-ghost"
                disabled=${busy}
                onClick=${requestClose}
              >
                Отмена
              </button>
              <button type="button" class="btn btn-primary btn-danger" disabled=${busy} onClick=${handleConfirm}>
                Забыть
              </button>
            </div>
          `
        : null}
    </dialog>
  `;
}

export function Memory() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [pinnedCount, setPinnedCount] = useState(0);
  const [pinnedMax, setPinnedMax] = useState(0);
  const [loadFailed, setLoadFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [kindFilter, setKindFilter] = useState(null); // null = "Все"
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [filterLoading, setFilterLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [showAddForm, setShowAddForm] = useState(false);
  // {item, trigger} | null -- the pending "Забыть" confirmation.
  const [forgetTarget, setForgetTarget] = useState(null);
  const [forgetBusy, setForgetBusy] = useState(false);
  const skipFilterEffect = useRef(true);
  // Bumped by every *replacing* fetch (reload, a filter change,
  // fetchAndSet's own non-append calls); a response is applied only if
  // this hasn't moved on since, and an in-flight append (loadMore) is
  // dropped if either this or filterKeyRef has (W3 finding: without
  // this, whichever of reload/loadMore/a filter change happened to
  // resolve *last* won, including appending one filter's page onto
  // another's list).
  const seqRef = useRef(0);
  // Kept in a ref, updated every render (the same pattern hooks.js's
  // useAutoRefetch uses for `reload`), so an in-flight fetch started
  // under one filter can tell it is now stale even though its own
  // closure still has the old kindFilter/pinnedOnly values.
  const filterKeyRef = useRef(null);
  filterKeyRef.current = `${kindFilter}|${pinnedOnly}`;
  // The id `editMemory` just replaced, and what to give focus back to
  // once the new row lands in `items` -- the keyed MemoryCard for the
  // old id unmounts and the new one mounts with editing=false, so
  // MemoryCard's own focus-return effect never runs (W3 finding); see
  // the effect below that consumes this.
  const focusAfterEditRef = useRef(null);

  function query(offset, limit) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (kindFilter) params.set('kind', kindFilter);
    if (pinnedOnly) params.set('pinned', 'true');
    return `/api/memories?${params.toString()}`;
  }

  async function fetchAndSet(offset, limit, append) {
    // Only a *replacing* fetch claims a new sequence number -- an
    // append (loadMore) rides whatever sequence is already current and
    // is dropped below if a replacing fetch (or a filter change) moves
    // past it before this resolves.
    const seq = append ? seqRef.current : ++seqRef.current;
    const filterKeyAtCall = filterKeyRef.current;
    const res = await apiGet(query(offset, limit));
    if (res.status === 401) {
      forceLogout();
      return false;
    }
    if (seq !== seqRef.current || (append && filterKeyAtCall !== filterKeyRef.current)) {
      return false;
    }
    if (res.ok && res.data) {
      setItems((prev) => (append ? [...prev, ...res.data.items] : res.data.items));
      setTotal(res.data.total);
      setPinnedCount(res.data.pinned_count);
      setPinnedMax(res.data.pinned_max);
      setLoadFailed(false);
      setLoaded(true);
      return true;
    }
    if (!append) setLoadFailed(true);
    return false;
  }

  // Triggered by mount, SSE invalidate("memory"), '*' (reconnect) and
  // tab-visibility (useAutoRefetch below) -- refetches however many
  // rows are already loaded instead of collapsing back to the first
  // page, so a background refresh does not undo a "Показать ещё" click
  // (every write this screen itself makes is followed by exactly such
  // a refresh -- the backend's invalidate("memory") after every write).
  // Above the endpoint's own MAX_LIMIT-per-request cap this fetches in
  // several chunks and applies them together in one setItems, rather
  // than the single min(items.length, MAX_LIMIT) request the endpoint
  // cap alone allows -- otherwise every such refresh cut a longer list
  // back to 50 rows, dropping whatever was scrolled to (W3 finding).
  async function reload() {
    const target = Math.max(items.length, PAGE_SIZE);
    if (target <= MAX_LIMIT) {
      await fetchAndSet(0, target, false);
      return;
    }
    const seq = ++seqRef.current;
    const filterKeyAtCall = filterKeyRef.current;
    const collected = [];
    let last = null;
    for (let offset = 0; offset < target; offset += MAX_LIMIT) {
      const chunkLimit = Math.min(MAX_LIMIT, target - offset);
      const res = await apiGet(query(offset, chunkLimit));
      if (res.status === 401) {
        forceLogout();
        return;
      }
      if (seq !== seqRef.current || filterKeyAtCall !== filterKeyRef.current) return;
      if (!res.ok || !res.data) return;
      collected.push(...res.data.items);
      last = res.data;
      if (res.data.items.length < chunkLimit) break; // fewer rows left than the cap covers
    }
    if (!last) return;
    setItems(collected);
    setTotal(last.total);
    setPinnedCount(last.pinned_count);
    setPinnedMax(last.pinned_max);
    setLoadFailed(false);
    setLoaded(true);
  }

  useAutoRefetch('memory', reload);

  // A filter change (kind chip, pinned toggle) is a fresh query, not a
  // refresh -- reset to the first page rather than trying to preserve
  // however many rows were loaded under the *previous* filter.
  // `skipFilterEffect` swallows the run this effect would otherwise
  // make on mount (useAutoRefetch's own mount effect above already
  // fetches once); every render after that is a real filter change.
  useEffect(() => {
    if (skipFilterEffect.current) {
      skipFilterEffect.current = false;
      return;
    }
    seqRef.current += 1; // orphan any fetch (e.g. a loadMore) already in flight
    setItems([]);
    setFilterLoading(true);
    fetchAndSet(0, PAGE_SIZE, false).finally(() => setFilterLoading(false));
    // eslint-disable-next-line
  }, [kindFilter, pinnedOnly]);

  async function loadMore() {
    setLoadingMore(true);
    const ok = await fetchAndSet(items.length, PAGE_SIZE, true);
    setLoadingMore(false);
    if (!ok) pushToast('Не удалось загрузить.');
  }

  async function addMemory(kind, text) {
    const res = await apiPost('/api/memories', { kind, text });
    if (res.status === 401) {
      forceLogout();
      return 'Сессия истекла.';
    }
    if (res.status === 201 && res.data && res.data.memory) {
      const created = res.data.memory;
      const matchesFilter = (!kindFilter || created.kind === kindFilter) && (!pinnedOnly || created.pinned);
      if (matchesFilter) {
        setItems((prev) => [created, ...prev]);
        setTotal((n) => n + 1);
      }
      pushToast('Запомнено.');
      return true;
    }
    if (res.status === 409) {
      return { duplicate: (res.data && res.data.existing) || null };
    }
    if (res.status === 422) {
      const detail = res.data && res.data.detail;
      return DETAIL_MESSAGES[detail] || 'Неверное значение.';
    }
    if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
      return 'Слишком много попыток.';
    }
    return 'Не сохранено.';
  }

  async function editMemory(id, text) {
    const res = await apiPost(`/api/memories/${id}/edit`, { text });
    if (res.status === 401) {
      forceLogout();
      return 'Сессия истекла.';
    }
    if (res.status === 200 && res.data && res.data.memory) {
      const updated = res.data.memory;
      // The edited row gets a new id (write_memory never updates in
      // place), so the keyed MemoryCard for `id` unmounts and a fresh
      // one mounts for `updated.id` with editing=false -- its own
      // focus-return effect never runs. Recorded here and consumed by
      // the effect below, once `updated` actually lands in `items`.
      focusAfterEditRef.current = updated.id;
      setItems((prev) => prev.map((it) => (it.id === id ? updated : it)));
      pushToast('Исправлено.');
      return true;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      setItems((prev) => prev.filter((it) => it.id !== id));
      return 'Уже неактуально.';
    }
    if (res.status === 409) {
      return { duplicate: (res.data && res.data.existing) || null };
    }
    if (res.status === 422) {
      const detail = res.data && res.data.detail;
      return DETAIL_MESSAGES[detail] || 'Неверное значение.';
    }
    if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
      return 'Слишком много попыток.';
    }
    return 'Не сохранено.';
  }

  // Consumes focusAfterEditRef once the row it names has actually
  // landed in `items` (the ref is set synchronously in editMemory,
  // ahead of the setItems call above landing in the DOM) -- see
  // editMemory's own comment for why this is needed at all.
  useEffect(() => {
    const id = focusAfterEditRef.current;
    if (id == null) return;
    focusAfterEditRef.current = null;
    const button = document.querySelector(`[data-memory-id="${id}"]`);
    if (button) button.focus();
  }, [items]);

  async function pinMemory(item) {
    const action = item.pinned ? 'unpin' : 'pin';
    const res = await apiPost(`/api/memories/${item.id}/${action}`, {});
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (res.status === 200 && res.data && res.data.memory) {
      const updated = res.data.memory;
      setItems((prev) => prev.map((it) => (it.id === item.id ? updated : it)));
      // Compare the server's confirmed `pinned` against what this card
      // showed *before* the request, not against `action` -- an
      // idempotent pin/unpin (this row was already in that state, e.g.
      // a race with a Telegram /pin) reports 200 without actually
      // changing the count.
      if (updated.pinned !== item.pinned) {
        setPinnedCount((n) => Math.max(0, n + (updated.pinned ? 1 : -1)));
      }
      return;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      setItems((prev) => prev.filter((it) => it.id !== item.id));
      return;
    }
    if (res.status === 409) {
      const max = res.data && res.data.max;
      pushToast(`Закреплено уже ${max} — открепи что-нибудь`);
      return;
    }
    if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
      return;
    }
    pushToast('Не удалось сохранить.');
  }

  function openForget(item, triggerRef) {
    setForgetTarget({ item, trigger: triggerRef.current });
  }

  // Clears the pending target; ForgetDialog itself owns closing the
  // native <dialog> and returning focus (see its own onNativeClose) --
  // this is also what that close event calls back into, so it must
  // stay idempotent (a no-op once `forgetTarget` is already null).
  function cancelForget() {
    setForgetTarget(null);
  }

  // Does the request and the list bookkeeping only -- ForgetDialog's
  // handleConfirm awaits this, then closes the dialog itself, which is
  // what actually clears `forgetTarget` and returns focus (via
  // cancelForget/onNativeClose above).
  async function confirmForget() {
    if (!forgetTarget) return;
    const { item } = forgetTarget;
    setForgetBusy(true);
    const res = await apiPost(`/api/memories/${item.id}/forget`, {});
    setForgetBusy(false);
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (res.status === 200 || res.status === 404) {
      setItems((prev) => prev.filter((it) => it.id !== item.id));
      setTotal((n) => Math.max(0, n - 1));
      if (item.pinned) setPinnedCount((n) => Math.max(0, n - 1));
      pushToast(res.status === 200 ? 'Забыто.' : 'Уже неактуально');
    } else if (res.status === 409 && res.data && res.data.error === 'adopted') {
      // app/core/memory.py's FORGET_PROTECTED: this row is the head of
      // a chain a StudyCard still points at (an adopted technique), so
      // the backend refused rather than strand it -- the item stays in
      // the list, same as any other failed write.
      pushToast('Эта запись — часть принятой техники, так её не забыть.');
    } else if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
    } else {
      pushToast('Не удалось сохранить.');
    }
  }

  const displayed = items
    .filter((it) => {
      if (!search.trim()) return true;
      return it.text.toLowerCase().includes(search.trim().toLowerCase());
    })
    // Pinned first; Array#sort is stable (ES2019+), so relative order
    // within each group is otherwise whatever the server returned.
    .slice()
    .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));

  const hasMore = items.length < total;
  const overCap = pinnedMax > 0 && pinnedCount >= pinnedMax;
  // «Пока ничего не запомнено» claims memory is empty outright -- true
  // only with no kind/pinned/search filter narrowing the view. Any
  // filter or search that happens to match nothing gets «Ничего не
  // найдено» instead (W3 finding: the two were conflated, so toggling
  // e.g. «Закреплённые» on an unpinned-only memory claimed there was
  // nothing memorized at all).
  const hasActiveFilter = Boolean(kindFilter) || pinnedOnly || Boolean(search.trim());
  const emptyText = hasActiveFilter ? 'Ничего не найдено' : 'Пока ничего не запомнено';

  if (loadFailed && !loaded) {
    return html`
      <div class="screen-wrap">
        <div class="screen">
          <p class="empty-hint">
            Не удалось загрузить.
            <button type="button" class="link-button" onClick=${reload}>Повторить</button>
          </p>
        </div>
        <${Toasts} />
      </div>
    `;
  }
  if (!loaded) {
    return html`<div class="screen-wrap"><div class="screen" aria-busy="true"></div></div>`;
  }

  return html`
    <div class="screen-wrap">
      <div class="screen screen-memory">
        <div class="card-row">
          <p class="memory-counter${overCap ? ' char-counter-over' : ''}" aria-live="polite">
            <span class="mono">${total}</span> записей · закреплено
            <span class="mono">${pinnedCount}/${pinnedMax}</span>
          </p>
          <button
            type="button"
            id="memory-add-toggle"
            class="btn btn-primary"
            onClick=${() => setShowAddForm((v) => !v)}
          >
            ${showAddForm ? 'Закрыть' : '+ Добавить'}
          </button>
        </div>
        <${AddForm} open=${showAddForm} onClose=${() => setShowAddForm(false)} onAdd=${addMemory} />
        <div class="filters">
          <div class="chip-row" role="group" aria-label="Вид">
            <button
              type="button"
              class="chip"
              aria-pressed=${kindFilter === null ? 'true' : 'false'}
              onClick=${() => setKindFilter(null)}
            >
              Все
            </button>
            ${KIND_ORDER.map(
              (k) => html`
                <button
                  key=${k}
                  type="button"
                  class="chip"
                  aria-pressed=${kindFilter === k ? 'true' : 'false'}
                  onClick=${() => setKindFilter(k)}
                >
                  ${KIND_LABELS[k]}
                </button>
              `,
            )}
          </div>
          <button
            type="button"
            class="chip"
            aria-pressed=${pinnedOnly ? 'true' : 'false'}
            onClick=${() => setPinnedOnly((v) => !v)}
          >
            Закреплённые
          </button>
          <div class="search-field">
            <label for="memory-search" class="sr-only">Поиск</label>
            <div class="search-input-wrap">
              <span class="search-icon"><${Icon} name="search" size=${16} /></span>
              <input
                id="memory-search"
                type="search"
                placeholder="Поиск"
                value=${search}
                onInput=${(e) => setSearch(e.target.value)}
              />
            </div>
            <p class="field-hint">ищет в загруженных</p>
          </div>
        </div>
        ${displayed.length
          ? html`
              <ul class="memory-list">
                ${displayed.map(
                  (item) => html`
                    <${MemoryCard}
                      key=${item.id}
                      item=${item}
                      forgetBusy=${forgetBusy}
                      onPin=${pinMemory}
                      onEdit=${editMemory}
                      onForgetClick=${openForget}
                    />
                  `,
                )}
              </ul>
            `
          : html`<p class="empty-hint" aria-busy=${filterLoading ? 'true' : 'false'}>${filterLoading ? '' : emptyText}</p>`}
        ${hasMore
          ? html`
              <button type="button" class="btn" disabled=${loadingMore} onClick=${loadMore}>
                Показать ещё
              </button>
            `
          : null}
      </div>
      <${ForgetDialog}
        target=${forgetTarget}
        busy=${forgetBusy}
        onCancel=${cancelForget}
        onConfirm=${confirmForget}
      />
      <${Toasts} />
    </div>
  `;
}
