// Сегодня's «Долги» card: the debt queue (app/core/obligations.py) --
// things the user still owes, oldest first, each closed as done
// («Сделано») or dropped («Снять», behind a confirmation). The same
// queue as Telegram's /paid; the persona's next order closes one of
// these before inventing anything new. Hidden while the queue is
// empty.
import { html } from '../../html.js';
import { useRef, useState } from '../../../vendor/hooks.module.js';
import { pushToast } from '../../store.js';
import { useMountedRef, useResource } from '../../hooks.js';
import { send } from '../../lib/request.js';
import { parseLocalDate, shortDate } from '../../lib/dates.js';
import { dayWordInTz } from '../../lib/tz.js';
import { ConfirmDialog } from '../../ui/ConfirmDialog.js';

// app/core/obligations.py's KINDS: where a debt came from.
const KIND_LABELS = {
  checkin: 'чек-ин',
  focus: 'действие дня',
  promised: 'обещание',
  missed: 'пропуск',
  custom: 'своё',
};

// GET /api/obligations, refetched on invalidate("debts") -- published
// by this card's own closes and by the tail when Telegram's /paid (or
// /due, a promise, the missed-check-in sweep) moves the queue.
export function useDebts() {
  const { data, failed, reload } = useResource('/api/obligations', 'debts', {
    accept: (d) => Array.isArray(d.items),
  });

  // 'done' or 'drop'. Returns whether the row is gone.
  async function close(id, action) {
    const res = await send(`/api/obligations/${id}/${action}`, {}, { fallback: 'Не удалось сохранить.' });
    if (res.ok) {
      pushToast(action === 'done' ? 'Закрыто.' : 'Снято.');
      return true;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      reload();
      return true;
    }
    if (res.status !== 401) pushToast(res.error);
    return false;
  }

  return { data, failed, reload, close };
}

function DebtRow({ item, timezone, onDone, onDropClick }) {
  const [busy, setBusy] = useState(false);
  const mounted = useMountedRef();
  const dropRef = useRef(null);

  async function done() {
    setBusy(true);
    await onDone(item.id);
    if (mounted.current) setBusy(false);
  }

  const due = item.due_local_date ? parseLocalDate(item.due_local_date) : NaN;
  const hint = [
    KIND_LABELS[item.kind] || item.kind,
    `с ${dayWordInTz(item.opened_at, timezone)}`,
    Number.isNaN(due) ? null : `до ${shortDate(due)}`,
  ]
    .filter(Boolean)
    .join(' · ');

  return html`
    <li class="row debt-row">
      <div class="row-main">
        <p class="field-value">${item.text}</p>
        <span class="field-hint">${hint}</span>
      </div>
      <div class="card-footer-actions">
        <button
          type="button"
          ref=${dropRef}
          class="btn btn-ghost"
          disabled=${busy}
          onClick=${() => onDropClick(item, dropRef)}
        >
          Снять
        </button>
        <button type="button" class="btn" disabled=${busy} onClick=${done}>Сделано</button>
      </div>
    </li>
  `;
}

export function DebtsCard({ debts, timezone }) {
  // {item, trigger} | null -- the pending «Снять» confirmation.
  const [dropTarget, setDropTarget] = useState(null);
  const [dropBusy, setDropBusy] = useState(false);
  const items = debts.data ? debts.data.items : [];
  if (!items.length && !dropTarget) return null;

  async function confirmDrop() {
    if (!dropTarget) return;
    setDropBusy(true);
    await debts.close(dropTarget.item.id, 'drop');
    setDropBusy(false);
  }

  return html`
    <section class="card" aria-labelledby="debts-heading" id="debts-card">
      <div class="card-row">
        <h2 id="debts-heading">Долги</h2>
        <span class="field-hint">
          <span class="mono">${items.length}</span> из <span class="mono">${debts.data.max_open}</span>
        </span>
      </div>
      <ul class="card-list">
        ${items.map(
          (item) => html`
            <${DebtRow}
              key=${item.id}
              item=${item}
              timezone=${timezone}
              onDone=${(id) => debts.close(id, 'done')}
              onDropClick=${(it, ref) => setDropTarget({ item: it, trigger: ref.current })}
            />
          `,
        )}
      </ul>
      <${ConfirmDialog}
        target=${dropTarget}
        busy=${dropBusy}
        heading="Снять долг?"
        confirmLabel="Снять"
        fallbackFocusId="debts-heading"
        onClose=${() => setDropTarget(null)}
        onConfirm=${confirmDrop}
      >
        ${dropTarget ? html`<p class="field-hint">«${dropTarget.item.text}»</p>` : null}
        <p>Он пропадёт из списка, и Echo перестанет о нём напоминать.</p>
      <//>
    </section>
  `;
}
