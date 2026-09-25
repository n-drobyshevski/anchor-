// The proposals screen (#/proposals, nav label "Предложения"): the
// pending proposal (if any), with Accept/Reject, and the last 20
// decided ones below it. This is also the only place `store.js`'s
// `proposalsBadge` signal gets its numbers from besides main.js's own
// after-login fetch -- see that module's comment for why both exist.
import { html } from '../html.js';
import { useState } from '../../vendor/hooks.module.js';
import { apiGet, apiPost } from '../api.js';
import { forceLogout, proposalsBadge, pushToast } from '../store.js';
import { useAutoRefetch } from '../hooks.js';
import { Toasts } from '../ui/Toasts.js';

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

function minutesText(retryAfterSeconds) {
  const m = Math.max(1, Math.ceil((retryAfterSeconds || 60) / 60));
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

function formatDateTime(iso) {
  return new Date(iso).toLocaleString('ru-RU');
}

function PendingCard({ proposal, onDecide }) {
  const [busy, setBusy] = useState(false);

  async function decide(action) {
    setBusy(true);
    await onDecide(proposal.id, action);
    setBusy(false);
  }

  return html`
    <section class="card proposal-card" aria-labelledby="pending-heading">
      <h2 id="pending-heading">${proposal.field_label}</h2>
      <p class="field-value">${displayValue(proposal.field, proposal.value)}</p>
      ${proposal.reason ? html`<p class="field-hint">Почему: ${proposal.reason}</p>` : null}
      <div class="btn-row">
        <button type="button" class="btn btn-primary" disabled=${busy} onClick=${() => decide('accept')}>
          Принять
        </button>
        <button type="button" class="btn btn-ghost" disabled=${busy} onClick=${() => decide('reject')}>
          Отклонить
        </button>
      </div>
    </section>
  `;
}

function HistoryRow({ item }) {
  return html`
    <li class="history-row">
      <div>
        <p class="field-value">${item.field_label}: ${displayValue(item.field, item.value)}</p>
        <p class="field-hint">${formatDateTime(item.decided_at || item.created_at)}</p>
      </div>
      <span class="status-chip" data-status=${item.status}>${STATUS_LABELS[item.status] || item.status}</span>
    </li>
  `;
}

export function Proposals() {
  const [data, setData] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);

  async function reload() {
    const res = await apiGet('/api/proposals');
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (res.ok && res.data) {
      setData(res.data);
      setLoadFailed(false);
      // Kept in sync here too (not only in main.js's own after-login/
      // invalidate fetch), so the badge is exactly right the instant
      // this screen's own reload lands, without waiting on a second
      // round-trip main.js would otherwise make on the same event.
      proposalsBadge.value = res.data.pending ? 1 : 0;
    } else {
      setLoadFailed(true);
    }
  }

  useAutoRefetch('proposals', reload);

  async function decide(id, action) {
    const res = await apiPost(`/api/proposals/${id}/${action}`, {});
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (res.status === 200) {
      pushToast(action === 'accept' ? 'Принято.' : 'Отклонено.');
      await reload();
      return;
    }
    if (res.status === 404) {
      pushToast('Уже неактуально');
      await reload();
      return;
    }
    if (res.status === 409) {
      const stale = res.data && res.data.proposal;
      const label = stale ? STATUS_LABELS[stale.status] || stale.status : 'неактуально';
      pushToast(`Уже ${label}`);
      await reload();
      return;
    }
    if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
      return;
    }
    pushToast('Не удалось сохранить.');
  }

  if (loadFailed) {
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
  if (!data) {
    return html`<div class="screen-wrap"><div class="screen" aria-busy="true"></div></div>`;
  }

  return html`
    <div class="screen-wrap">
      <div class="screen screen-proposals">
        ${data.pending
          ? html`<${PendingCard} proposal=${data.pending} onDecide=${decide} />`
          : html`<p class="empty-hint">Сейчас предложений нет</p>`}
        <section class="card">
          <h2>История</h2>
          ${data.recent.length
            ? html`
                <ul class="history-list">
                  ${data.recent.map((item) => html`<${HistoryRow} key=${item.id} item=${item} />`)}
                </ul>
              `
            : html`<p class="field-hint">Пока пусто</p>`}
        </section>
      </div>
      <${Toasts} />
    </div>
  `;
}
