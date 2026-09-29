// Память's «Договорённости» tab: standing orders (GET /api/orders) --
// the user's recurring commitments, asked about in every check-in. The
// same list as Telegram's /orders: add one (`/order <каденция>
// <текст>`, the same cadences) and retire one ([Снять]).
import { html } from '../../html.js';
import { useRef, useState } from '../../../vendor/hooks.module.js';
import { pushToast } from '../../store.js';
import { useResource } from '../../hooks.js';
import { send } from '../../lib/request.js';
import { ConfirmDialog } from '../../ui/ConfirmDialog.js';
import { LoadError } from '../../ui/ScreenState.js';

// app/core/orders.py's parse_cadence tokens, in /order's own terms.
const CADENCES = [
  { token: 'daily', label: 'Каждый день' },
  { token: 'weekdays', label: 'По будням' },
  { token: 'weekly:1', label: 'По понедельникам' },
  { token: 'weekly:2', label: 'По вторникам' },
  { token: 'weekly:3', label: 'По средам' },
  { token: 'weekly:4', label: 'По четвергам' },
  { token: 'weekly:5', label: 'По пятницам' },
  { token: 'weekly:6', label: 'По субботам' },
  { token: 'weekly:7', label: 'По воскресеньям' },
  { token: 'once', label: 'Один раз' },
];

const ORDER_DETAILS = { bad_cadence: 'Неверная периодичность.', empty: 'Пусто.' };

function AddOrder({ textMax, full, onAdd }) {
  const [text, setText] = useState('');
  const [cadence, setCadence] = useState('daily');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const trimmed = text.trim();

  async function submit(e) {
    e.preventDefault();
    if (!trimmed || busy) return;
    setBusy(true);
    setError('');
    const result = await onAdd(trimmed, cadence);
    setBusy(false);
    if (result === true) setText('');
    else setError(result);
  }

  return html`
    <form class="inline-add" onSubmit=${submit}>
      <label for="order-add" class="sr-only">Новая договорённость</label>
      <input
        id="order-add"
        type="text"
        placeholder=${full ? 'Сначала сними одну из договорённостей' : 'Новая договорённость'}
        maxlength=${textMax}
        disabled=${busy || full}
        value=${text}
        onInput=${(e) => setText(e.target.value)}
      />
      <label for="order-cadence" class="sr-only">Как часто</label>
      <select id="order-cadence" disabled=${busy || full} value=${cadence} onChange=${(e) => setCadence(e.target.value)}>
        ${CADENCES.map((c) => html`<option key=${c.token} value=${c.token}>${c.label}</option>`)}
      </select>
      <button type="submit" class="btn btn-primary" disabled=${busy || full || !trimmed}>Добавить</button>
      ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
    </form>
  `;
}

function OrderRow({ item, onRetire }) {
  const ref = useRef(null);
  return html`
    <li class="row">
      <div class="row-main">
        <p class="field-value">${item.text}</p>
        <span class="field-hint">${item.cadence_label}</span>
      </div>
      <button type="button" ref=${ref} class="btn btn-ghost" onClick=${() => onRetire({ item, trigger: ref.current })}>
        Снять
      </button>
    </li>
  `;
}

export function OrdersTab() {
  const { data, failed, reload } = useResource('/api/orders', 'orders', { accept: (d) => Array.isArray(d.items) });
  const [target, setTarget] = useState(null);
  const [busy, setBusy] = useState(false);

  async function add(text, cadence) {
    const res = await send('/api/orders', { text, cadence });
    if (res.ok) {
      pushToast('Договорились.');
      return true;
    }
    const detail = res.data && res.data.detail;
    return (res.data && res.data.message) || ORDER_DETAILS[detail] || res.error;
  }

  async function confirmRetire() {
    if (!target) return;
    setBusy(true);
    const res = await send(`/api/orders/${target.item.id}/retire`, {}, { fallback: 'Не удалось сохранить.' });
    setBusy(false);
    if (res.ok) pushToast('Снято.');
    else if (res.status === 404) {
      pushToast('Уже неактуально');
      reload();
    } else if (res.status !== 401) pushToast(res.error);
  }

  if (!data) return failed ? html`<${LoadError} onRetry=${reload} />` : html`<div aria-busy="true"></div>`;

  const full = data.items.length >= data.limits.active_max;

  return html`
    <div class="memory-tab">
      <section class="card" aria-labelledby="orders-heading">
        <div class="card-row">
          <h2 id="orders-heading">Договорённости</h2>
          <span class="field-hint"><span class="mono">${data.items.length}</span> из <span class="mono">${data.limits.active_max}</span></span>
        </div>
        <p class="field-hint">Что ты обещал делать регулярно. Echo спрашивает о них в каждом чек-ине.</p>
        ${data.items.length
          ? html`
              <ul class="card-list">
                ${data.items.map((item) => html`<${OrderRow} key=${item.id} item=${item} onRetire=${setTarget} />`)}
              </ul>
            `
          : html`<p class="field-hint">Договорённостей пока нет.</p>`}
        <${AddOrder} textMax=${data.limits.text_max} full=${full} onAdd=${add} />
      </section>
      <${ConfirmDialog}
        target=${target}
        busy=${busy}
        heading="Снять договорённость?"
        confirmLabel="Снять"
        fallbackFocusId="memory-tab-orders"
        onClose=${() => setTarget(null)}
        onConfirm=${confirmRetire}
      >
        ${target ? html`<p class="field-hint">«${target.item.text}» — ${target.item.cadence_label}</p>` : null}
        <p>Echo перестанет о ней спрашивать. Это не штраф — просто снимаем.</p>
      <//>
    </div>
  `;
}
