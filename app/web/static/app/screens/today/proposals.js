// Proposals (a change Echo suggests to the state, decided with
// Принять/Отклонить), moved out of the old Proposals screen: the
// pending card now sits on Сегодня, the decided history on Дневник.
import { html } from '../../html.js';
import { useEffect, useState } from '../../../vendor/hooks.module.js';
import { proposalsBadge, pushToast } from '../../store.js';
import { useMountedRef, useResource } from '../../hooks.js';
import { send } from '../../lib/request.js';
import { formatDateTime } from '../../lib/format.js';

// focus_on values arrive raw ("on", "вкл", ...); show them the way
// app/core/proposal.py's parse_focus reads them.
const FOCUS_ON_VALUES = new Set(['on', 'вкл', 'включить', 'true', '1', 'да']);

function displayValue(field, value) {
  if (field === 'focus_on') {
    return FOCUS_ON_VALUES.has(String(value).trim().toLowerCase()) ? 'включить' : 'выключить';
  }
  return value;
}

const STATUS_LABELS = {
  pending: 'ожидает',
  accepted: 'принято',
  rejected: 'отклонено',
  expired: 'истекло',
};

export function PendingCard({ proposal, onDecide }) {
  const [busy, setBusy] = useState(false);
  // A successful decision's invalidate("proposals") can replace (and
  // unmount) this card before the request's own await resolves.
  const mounted = useMountedRef();

  async function decide(action) {
    setBusy(true);
    await onDecide(proposal.id, action);
    if (mounted.current) setBusy(false);
  }

  return html`
    <section class="card proposal-card" aria-labelledby="pending-heading">
      <h2 id="pending-heading">${proposal.field_label}</h2>
      <p class="field-value value-strong">${displayValue(proposal.field, proposal.value)}</p>
      ${proposal.reason ? html`<p class="field-hint">Почему: ${proposal.reason}</p>` : null}
      <div class="card-footer">
        <span></span>
        <div class="card-footer-actions">
          <button type="button" class="btn" disabled=${busy} onClick=${() => decide('reject')}>
            Отклонить
          </button>
          <button type="button" class="btn btn-primary" disabled=${busy} onClick=${() => decide('accept')}>
            Принять
          </button>
        </div>
      </div>
    </section>
  `;
}

export function HistoryRow({ item }) {
  return html`
    <li class="row">
      <div class="row-main">
        <p class="field-value">${item.field_label}: ${displayValue(item.field, item.value)}</p>
        <p class="field-hint mono">${formatDateTime(item.decided_at || item.created_at)}</p>
      </div>
      <span class="status-chip" data-status=${item.status}>${STATUS_LABELS[item.status] || item.status}</span>
    </li>
  `;
}

// GET /api/proposals, shared by Сегодня (the pending card) and Дневник
// (the decided history): {data, failed, reload, decide}. Also keeps
// store.js's proposalsBadge in step with its own loads, so the badge
// does not wait on main.js's round-trip for the same event.
export function useProposals() {
  const { data, failed, reload } = useResource('/api/proposals', 'proposals');

  useEffect(() => {
    if (data) proposalsBadge.value = data.pending ? 1 : 0;
  }, [data]);

  async function decide(id, action) {
    const res = await send(`/api/proposals/${id}/${action}`, {});
    if (res.status === 200) {
      // No reload here: the endpoint publishes invalidate("proposals"),
      // which refetches (and main.js's badge) once.
      pushToast(action === 'accept' ? 'Принято.' : 'Отклонено.');
      return;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      reload();
      return;
    }
    if (res.status === 409) {
      const stale = res.data && res.data.proposal;
      const label = stale ? STATUS_LABELS[stale.status] || stale.status : 'неактуально';
      pushToast(`Уже ${label}`);
      reload();
      return;
    }
    if (res.status !== 401) pushToast(res.status === 429 ? res.error : 'Не удалось сохранить.');
  }

  return { data, failed, reload, decide };
}
