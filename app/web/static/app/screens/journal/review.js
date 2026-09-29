// Дневник's weekly review and persona amendments.
//
// «Обзор недели» (GET /api/review): the latest weekly review's analysis
// -- what went well, what did not, patterns, the intentions it set --
// and its proposals, each decided here with Принять/Отклонить (the same
// app/core/review_actions.py path Telegram's buttons take). «Провести
// обзор» asks for one now, like /review: the persona's message about
// it arrives in the chat.
//
// «Поправки» (GET /api/amendments): the persona amendments in force,
// each revocable, as /amendments lists them.
import { html } from '../../html.js';
import { useEffect, useRef, useState } from '../../../vendor/hooks.module.js';
import { pushToast } from '../../store.js';
import { useMountedRef, useResource } from '../../hooks.js';
import { send } from '../../lib/request.js';
import { parseLocalDate, shortDate } from '../../lib/dates.js';
import { ConfirmDialog } from '../../ui/ConfirmDialog.js';
import { LoadError } from '../../ui/ScreenState.js';

const SECTIONS = [
  { key: 'wins', title: 'Получилось' },
  { key: 'misses', title: 'Не получилось' },
  { key: 'patterns', title: 'Закономерности' },
  { key: 'intentions', title: 'Намерения на неделю' },
];

const KIND_LABELS = { standing_order: 'Договорённость', persona_note: 'Поправка к стилю Echo' };
const STATUS_LABELS = { adopted: 'принято', rejected: 'отклонено', expired: 'истекло' };

// After «Провести обзор» the button stays disabled this long: the run
// (an analysis and a persona message) takes a while, and a second tap
// would only queue a second /review.
const RUN_COOLDOWN_MS = 60000;

function ProposalRow({ proposal, onDecide }) {
  const [busy, setBusy] = useState(false);
  const mounted = useMountedRef();

  async function decide(action) {
    setBusy(true);
    await onDecide(proposal.id, action);
    if (mounted.current) setBusy(false);
  }

  const pending = proposal.status === 'pending';
  const cadence = proposal.order ? ` · ${proposal.order.cadence_label}` : '';
  return html`
    <li class="row row-stacked">
      <div class="row-main">
        <span class="field-hint">${KIND_LABELS[proposal.kind] || proposal.kind}${cadence}</span>
        <p class="field-value">${proposal.text}</p>
        ${proposal.reason ? html`<span class="field-hint">Почему: ${proposal.reason}</span>` : null}
      </div>
      ${pending
        ? html`
            <div class="card-footer-actions">
              <button type="button" class="btn btn-ghost" disabled=${busy} onClick=${() => decide('reject')}>Отклонить</button>
              <button type="button" class="btn btn-primary" disabled=${busy} onClick=${() => decide('accept')}>Принять</button>
            </div>
          `
        : html`<span class="status-chip" data-status=${proposal.status}>${STATUS_LABELS[proposal.status] || proposal.status}</span>`}
    </li>
  `;
}

export function ReviewCard() {
  const { data, failed, reload } = useResource('/api/review', 'review');
  const [cooling, setCooling] = useState(false);
  const timerRef = useRef(null);
  useEffect(() => () => clearTimeout(timerRef.current), []);

  async function run() {
    setCooling(true);
    const res = await send('/api/review/run', {}, { fallback: 'Не удалось запустить.' });
    if (res.status === 202) {
      pushToast('Обзор готовится — Echo напишет в чат.');
      timerRef.current = setTimeout(() => setCooling(false), RUN_COOLDOWN_MS);
      return;
    }
    setCooling(false);
    if (res.status !== 401) pushToast(res.error);
  }

  async function decide(id, action) {
    const res = await send(`/api/review/proposals/${id}/${action}`, {}, { fallback: 'Не удалось сохранить.' });
    if (res.ok) {
      pushToast(action === 'accept' ? 'Принято.' : 'Отклонено.');
      return;
    }
    if (res.status === 409 && res.data && res.data.error === 'cap') pushToast(res.data.message);
    else if (res.status === 404 || res.status === 409) {
      pushToast('Уже неактуально');
      reload();
    } else if (res.status !== 401) pushToast(res.error);
  }

  let body;
  if (data === null) body = failed ? html`<${LoadError} onRetry=${reload} />` : html`<div aria-busy="true"></div>`;
  else if (!data.review) body = html`<p class="field-hint">Обзоров ещё не было. Echo подводит итоги недели сам, раз в неделю.</p>`;
  else {
    const review = data.review;
    const week = parseLocalDate(review.week_start);
    body = html`
      <p class="field-hint">Неделя с ${Number.isNaN(week) ? review.week_start : shortDate(week)}</p>
      ${SECTIONS.filter((s) => review[s.key].length).map(
        (s) => html`
          <div key=${s.key}>
            <h3 class="subheading">${s.title}</h3>
            <ul class="plain-list">
              ${review[s.key].map((line, i) => html`<li key=${i}>${line}</li>`)}
            </ul>
          </div>
        `,
      )}
      ${review.proposals.length
        ? html`
            <h3 class="subheading">Предложения</h3>
            <ul class="card-list">
              ${review.proposals.map((p) => html`<${ProposalRow} key=${p.id} proposal=${p} onDecide=${decide} />`)}
            </ul>
          `
        : null}
    `;
  }

  return html`
    <section class="card" aria-labelledby="review-heading" id="review-card">
      <div class="card-row">
        <h2 id="review-heading">Обзор недели</h2>
        <button type="button" class="btn" disabled=${cooling} onClick=${run}>
          ${cooling ? 'Готовится…' : 'Провести обзор'}
        </button>
      </div>
      ${body}
    </section>
  `;
}

export function AmendmentsCard() {
  const { data, failed, reload } = useResource('/api/amendments', 'review', { accept: (d) => Array.isArray(d.items) });
  const [target, setTarget] = useState(null);
  const [busy, setBusy] = useState(false);

  async function confirmRevoke() {
    if (!target) return;
    setBusy(true);
    const res = await send(`/api/amendments/${target.item.id}/revoke`, {}, { fallback: 'Не удалось сохранить.' });
    setBusy(false);
    if (res.ok) pushToast('Отозвано.');
    else if (res.status === 404) {
      pushToast('Уже неактуально');
      reload();
    } else if (res.status !== 401) pushToast(res.error);
  }

  let body;
  if (!data) body = failed ? html`<${LoadError} onRetry=${reload} />` : html`<div aria-busy="true"></div>`;
  else if (!data.items.length) body = html`<p class="field-hint">Поправок нет.</p>`;
  else {
    body = html`
      <ul class="card-list">
        ${data.items.map((item) => html`<${AmendmentRow} key=${item.id} item=${item} onRevoke=${setTarget} />`)}
      </ul>
    `;
  }

  return html`
    <section class="card" aria-labelledby="amendments-heading">
      <h2 id="amendments-heading">Поправки к стилю</h2>
      <p class="field-hint">Принятые предложения из обзоров: как Echo разговаривает с тобой. То же, что /amendments.</p>
      ${body}
      <${ConfirmDialog}
        target=${target}
        busy=${busy}
        heading="Отозвать поправку?"
        confirmLabel="Отозвать"
        fallbackFocusId="amendments-heading"
        onClose=${() => setTarget(null)}
        onConfirm=${confirmRevoke}
      >
        ${target ? html`<p class="field-hint">«${target.item.text}»</p>` : null}
        <p>Echo перестанет ей следовать.</p>
      <//>
    </section>
  `;
}

function AmendmentRow({ item, onRevoke }) {
  const ref = useRef(null);
  return html`
    <li class="row">
      <div class="row-main">
        <p class="field-value">${item.text}</p>
        ${item.stale ? html`<span class="field-hint">персона изменилась — проверь</span>` : null}
      </div>
      <button type="button" ref=${ref} class="btn btn-ghost" onClick=${() => onRevoke({ item, trigger: ref.current })}>
        Отозвать
      </button>
    </li>
  `;
}
