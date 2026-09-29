// Память (#/memory): three tabs, the tab kept in the hash
// (`#/memory?tab=notebook`) so a reload stays on it.
//
//   Факты          - memories, below (FactsTab).
//   Блокнот        - Echo's notebook, screens/memory/notebook.js.
//   Договорённости - standing orders, screens/memory/orders.js.
//
// Факты is the W3 HTTP contract's memories list (GET /api/memories),
// plus pin/unpin, "Исправить" (an inline edit that supersedes the row,
// exactly like Telegram's correction path), "Забыть" (hard delete,
// behind a confirmation) and an add form. Every mutation calls the same
// app/core/memory.py functions Telegram calls, and is silent in
// Telegram (audit source "web", no send). The list pages (offset/limit,
// «Показать ещё") through hooks.js's usePagedResource.
import { html } from '../html.js';
import { useEffect, useRef, useState } from '../../vendor/hooks.module.js';
import { pushToast, routeQuery } from '../store.js';
import { setRouteQuery } from '../router.js';
import { NotebookTab } from './memory/notebook.js';
import { OrdersTab } from './memory/orders.js';
import { usePagedResource } from '../hooks.js';
import { send } from '../lib/request.js';
import { formatDateTime, pluralRu } from '../lib/format.js';
import { CharCounter } from '../ui/CharCounter.js';
import { ConfirmDialog } from '../ui/ConfirmDialog.js';
import { Icon } from '../ui/Icon.js';
import { LoadError } from '../ui/ScreenState.js';
import { useInlineEdit } from '../ui/inlineEdit.js';

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
  extractor: 'Echo запомнил',
  adopt: 'техника',
  consolidate: 'Echo объединил',
};

const PAGE_SIZE = 30;
// The endpoint's own cap ("limit(<=50)" in the contract) -- reload()
// below cannot ask for more than this in one request no matter how
// many rows are already loaded.
const MAX_LIMIT = 50;

function kindLabel(item) {
  // The backend's own kind_label always wins for a real item -- this
  // only falls back to the local map if a future kind this build does
  // not know about ever reaches the DTO.
  return item.kind_label || KIND_LABELS[item.kind] || item.kind;
}

function MemoryCard({ item, onPin, onEdit, onForgetClick }) {
  const [duplicate, setDuplicate] = useState(null);
  const [pinBusy, setPinBusy] = useState(false);
  const forgetButtonRef = useRef(null);
  const edit = useInlineEdit(item.text, (text) => onEdit(item.id, text));
  const { editing, draft, setDraft, error, busy, textareaRef, openerRef } = edit;

  function startEdit() {
    setDuplicate(null);
    edit.start();
  }

  function cancelEdit() {
    setDuplicate(null);
    edit.cancel();
  }

  async function save() {
    setDuplicate(null);
    const result = await edit.save();
    if (result && typeof result === 'object') setDuplicate(result.duplicate);
  }

  const onKeyDown = (e) => edit.onKeyDown(e, save);

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
                ref=${openerRef}
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

function FactsTab() {
  const [kindFilter, setKindFilter] = useState(null); // null = "Все"
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [search, setSearch] = useState('');
  const [showAddForm, setShowAddForm] = useState(false);
  // {item, trigger} | null -- the pending "Забыть" confirmation.
  const [forgetTarget, setForgetTarget] = useState(null);
  const [forgetBusy, setForgetBusy] = useState(false);
  // The id `editMemory` just replaced: the keyed MemoryCard for the old
  // id unmounts and the new one mounts with editing=false, so its own
  // focus return never runs (W3 finding) -- the effect below does it
  // once the new row lands.
  const focusAfterEditRef = useRef(null);

  function query(offset, limit) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (kindFilter) params.set('kind', kindFilter);
    if (pinnedOnly) params.set('pinned', 'true');
    return `/api/memories?${params.toString()}`;
  }

  // Every write below is also followed by the backend's
  // invalidate("memory"), which refetches the loaded rows; the local
  // patches only make the change visible without waiting for it.
  const list = usePagedResource(query, 'memory', {
    key: `${kindFilter}|${pinnedOnly}`,
    pageSize: PAGE_SIZE,
    maxLimit: MAX_LIMIT,
  });
  const { items, total, loaded, failed, patch } = list;
  const pinnedCount = list.meta ? list.meta.pinned_count : 0;
  const pinnedMax = list.meta ? list.meta.pinned_max : 0;

  function setPinnedCount(fn) {
    patch((s) => ({ meta: s.meta && { ...s.meta, pinned_count: fn(s.meta.pinned_count) } }));
  }

  async function addMemory(kind, text) {
    const res = await send('/api/memories', { kind, text });
    if (res.status === 201 && res.data && res.data.memory) {
      const created = res.data.memory;
      const matchesFilter = (!kindFilter || created.kind === kindFilter) && (!pinnedOnly || created.pinned);
      if (matchesFilter) patch((s) => ({ items: [created, ...s.items], total: s.total + 1 }));
      pushToast('Запомнено.');
      return true;
    }
    if (res.status === 409) return { duplicate: (res.data && res.data.existing) || null };
    return res.error || 'Не сохранено.';
  }

  async function editMemory(id, text) {
    const res = await send(`/api/memories/${id}/edit`, { text });
    if (res.status === 200 && res.data && res.data.memory) {
      const updated = res.data.memory;
      focusAfterEditRef.current = updated.id;
      patch((s) => ({ items: s.items.map((it) => (it.id === id ? updated : it)) }));
      pushToast('Исправлено.');
      return true;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      patch((s) => ({ items: s.items.filter((it) => it.id !== id) }));
      return 'Уже неактуально.';
    }
    if (res.status === 409) return { duplicate: (res.data && res.data.existing) || null };
    return res.error || 'Не сохранено.';
  }

  useEffect(() => {
    const id = focusAfterEditRef.current;
    if (id == null) return;
    focusAfterEditRef.current = null;
    const button = document.querySelector(`[data-memory-id="${id}"]`);
    if (button) button.focus();
  }, [items]);

  async function pinMemory(item) {
    const action = item.pinned ? 'unpin' : 'pin';
    const res = await send(`/api/memories/${item.id}/${action}`, {}, { fallback: 'Не удалось сохранить.' });
    if (res.status === 200 && res.data && res.data.memory) {
      const updated = res.data.memory;
      patch((s) => ({ items: s.items.map((it) => (it.id === item.id ? updated : it)) }));
      // Compared against what the card showed *before* the request: an
      // idempotent pin/unpin (a race with a Telegram /pin) reports 200
      // without changing the count.
      if (updated.pinned !== item.pinned) {
        setPinnedCount((n) => Math.max(0, n + (updated.pinned ? 1 : -1)));
      }
      return;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      patch((s) => ({ items: s.items.filter((it) => it.id !== item.id) }));
      return;
    }
    if (res.status === 409) {
      pushToast(`Закреплено уже ${res.data && res.data.max} — открепи что-нибудь`);
      return;
    }
    if (res.status !== 401) pushToast(res.error);
  }

  function openForget(item, triggerRef) {
    setForgetTarget({ item, trigger: triggerRef.current });
  }

  // Must stay idempotent: ConfirmDialog calls it from the native
  // 'close' event, whichever way the dialog closed.
  function closeForget() {
    setForgetTarget(null);
  }

  async function confirmForget() {
    if (!forgetTarget) return;
    const { item } = forgetTarget;
    setForgetBusy(true);
    const res = await send(`/api/memories/${item.id}/forget`, {}, { fallback: 'Не удалось сохранить.' });
    setForgetBusy(false);
    if (res.status === 200 || res.status === 404) {
      patch((s) => ({ items: s.items.filter((it) => it.id !== item.id), total: Math.max(0, s.total - 1) }));
      if (item.pinned) setPinnedCount((n) => Math.max(0, n - 1));
      pushToast(res.status === 200 ? 'Забыто.' : 'Уже неактуально');
    } else if (res.status === 409 && res.data && res.data.error === 'adopted') {
      // app/core/memory.py's FORGET_PROTECTED: the head of a chain an
      // adopted StudyCard still points at.
      pushToast('Эта запись — часть принятой техники, так её не забыть.');
    } else if (res.status !== 401) {
      pushToast(res.error);
    }
  }

  const displayed = items
    .filter((it) => {
      if (!search.trim()) return true;
      return it.text.toLowerCase().includes(search.trim().toLowerCase());
    })
    // Pinned first. The server already orders pinned rows first; this
    // keeps a row pinned or unpinned here in the right group before
    // the refetch lands. Array#sort is stable, so the server's order
    // holds within each group.
    .slice()
    .sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));

  const hasMore = items.length < total;
  const overCap = pinnedMax > 0 && pinnedCount >= pinnedMax;
  // «Пока ничего не запомнено» only with no filter narrowing the view;
  // a filter or search that matches nothing gets «Ничего не найдено».
  const hasActiveFilter = Boolean(kindFilter) || pinnedOnly || Boolean(search.trim());
  const emptyText = hasActiveFilter ? 'Ничего не найдено' : 'Пока ничего не запомнено';

  if (!loaded) return failed ? html`<${LoadError} onRetry=${list.reload} />` : html`<div aria-busy="true"></div>`;

  let listBody;
  if (failed && !items.length) {
    // A filter change or refresh that failed after the first load: say
    // so, rather than «Ничего не найдено» over an empty list.
    listBody = html`<${LoadError} onRetry=${list.reload} />`;
  } else if (displayed.length) {
    listBody = html`
      <ul class="memory-list">
        ${displayed.map(
          (item) => html`
            <${MemoryCard}
              key=${item.id}
              item=${item}
              onPin=${pinMemory}
              onEdit=${editMemory}
              onForgetClick=${openForget}
            />
          `,
        )}
      </ul>
    `;
  } else {
    listBody = html`<p class="empty-hint" aria-busy=${list.switching ? 'true' : 'false'}>${list.switching ? '' : emptyText}</p>`;
  }

  return html`
    <div class="memory-tab">
        <div class="card-row">
          <p class="memory-counter${overCap ? ' char-counter-over' : ''}" aria-live="polite">
            <span class="mono">${total}</span> ${pluralRu(total, ['запись', 'записи', 'записей'])} · закреплено
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
        ${listBody}
        ${hasMore
          ? html`
              <button type="button" class="btn" disabled=${list.loadingMore} onClick=${list.loadMore}>
                Показать ещё
              </button>
            `
          : null}
      <${ConfirmDialog}
        target=${forgetTarget}
        busy=${forgetBusy}
        heading="Забыть навсегда?"
        confirmLabel="Забыть"
        fallbackFocusId="memory-add-toggle"
        onClose=${closeForget}
        onConfirm=${confirmForget}
      >
        ${forgetTarget
          ? html`
              <p class="field-hint">«${forgetTarget.item.text}»</p>
              ${forgetTarget.item.has_predecessor
                ? html`<p role="alert">У этой записи есть более ранние версии — они забудутся вместе с ней.</p>`
                : null}
              <p>Это нельзя отменить.</p>
            `
          : null}
      <//>
    </div>
  `;
}

// ---------- the screen: tabs ----------

const TABS = [
  { id: 'facts', label: 'Факты' },
  { id: 'notebook', label: 'Блокнот' },
  { id: 'orders', label: 'Договорённости' },
];

export function Memory() {
  const params = new URLSearchParams(routeQuery.value);
  const current = TABS.some((t) => t.id === params.get('tab')) ? params.get('tab') : 'facts';

  function choose(id) {
    setRouteQuery(id === 'facts' ? '' : `tab=${id}`);
  }

  // Arrow keys move between tabs (the WAI-ARIA tabs pattern); only the
  // selected tab is in the Tab order.
  function onKeyDown(e) {
    const i = TABS.findIndex((t) => t.id === current);
    let next = null;
    if (e.key === 'ArrowRight') next = TABS[(i + 1) % TABS.length];
    else if (e.key === 'ArrowLeft') next = TABS[(i - 1 + TABS.length) % TABS.length];
    else if (e.key === 'Home') next = TABS[0];
    else if (e.key === 'End') next = TABS[TABS.length - 1];
    if (!next) return;
    e.preventDefault();
    choose(next.id);
    const el = document.getElementById(`memory-tab-${next.id}`);
    if (el) el.focus();
  }

  let panel;
  if (current === 'notebook') panel = html`<${NotebookTab} />`;
  else if (current === 'orders') panel = html`<${OrdersTab} />`;
  else panel = html`<${FactsTab} />`;

  return html`
    <div class="screen-wrap">
      <div class="screen screen-memory">
        <div class="segmented memory-tabs" role="tablist" aria-label="Память" onKeyDown=${onKeyDown}>
          ${TABS.map(
            (t) => html`
              <button
                key=${t.id}
                id=${`memory-tab-${t.id}`}
                type="button"
                role="tab"
                aria-selected=${t.id === current ? 'true' : 'false'}
                aria-controls="memory-tabpanel"
                tabindex=${t.id === current ? '0' : '-1'}
                onClick=${() => choose(t.id)}
              >
                ${t.label}
              </button>
            `,
          )}
        </div>
        <div id="memory-tabpanel" role="tabpanel" aria-labelledby=${`memory-tab-${current}`} class="memory-tabpanel">
          ${panel}
        </div>
      </div>
    </div>
  `;
}

