// The proposals screen (#/proposals, nav label "Предложения"): the
// pending proposal (if any), with Accept/Reject, and the last 20
// decided ones below it. This is also the only place `store.js`'s
// `proposalsBadge` signal gets its numbers from besides main.js's own
// after-login fetch -- see that module's comment for why both exist.
import { html } from '../html.js';
import { useEffect, useState } from '../../vendor/hooks.module.js';
import { proposalsBadge, pushToast } from '../store.js';
import { useMountedRef, useResource } from '../hooks.js';
import { send } from '../lib/request.js';
import { formatDateTime } from '../lib/format.js';
import { ScreenError, ScreenLoading } from '../ui/ScreenState.js';

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

function PendingCard({ proposal, onDecide }) {
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

function HistoryRow({ item }) {
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

export function Proposals() {
  const { data, failed, reload } = useResource('/api/proposals', 'proposals');

  // Kept in sync here too (not only by main.js), so the badge is right
  // the instant this screen's own load lands.
  useEffect(() => {
    if (data) proposalsBadge.value = data.pending ? 1 : 0;
  }, [data]);

  async function decide(id, action) {
    const res = await send(`/api/proposals/${id}/${action}`, {});
    if (res.status === 200) {
      // No reload here: the endpoint publishes invalidate("proposals"),
      // which refetches this screen (and main.js's badge) once. An
      // explicit reload on top of it made three identical GETs.
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

  if (!data) return failed ? html`<${ScreenError} onRetry=${reload} />` : html`<${ScreenLoading} />`;

  return html`
    <div class="screen-wrap">
      <div class="screen screen-proposals">
        ${data.pending
          ? html`<${PendingCard} key=${data.pending.id} proposal=${data.pending} onDecide=${decide} />`
          : html`<p class="empty-hint">Сейчас предложений нет</p>`}
        <section class="card" aria-labelledby="proposals-history-heading">
          <h2 id="proposals-history-heading">История</h2>
          ${data.recent.length
            ? html`
                <ul class="card-list">
                  ${data.recent.map((item) => html`<${HistoryRow} key=${item.id} item=${item} />`)}
                </ul>
              `
            : html`<p class="field-hint">Пока пусто</p>`}
        </section>
      </div>
    </div>
  `;
}
