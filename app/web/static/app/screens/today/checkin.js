// Сегодня's check-in section, moved out of the old Check-in screen
// (W4 HTTP contract: GET/POST /api/checkin): the form, or a summary of
// today's finished check-in with «Пройти заново», or «Отправлено» while
// a web-submitted one is still waiting for the worker.
import { html } from '../../html.js';
import { useEffect, useRef, useState } from '../../../vendor/hooks.module.js';
import { pushToast } from '../../store.js';
import { useResource } from '../../hooks.js';
import { detailText, send } from '../../lib/request.js';
import { pluralRu } from '../../lib/format.js';
import { CharCounter } from '../../ui/CharCounter.js';

// Fallback only -- the form DTO carries the real cap as `note_max`.
const NOTE_MAX_DEFAULT = 500;

export const RATINGS = [1, 2, 3, 4, 5];

const DUE_OPTIONS = [
  { value: 'done', label: 'Да' },
  { value: 'partial', label: 'Частично' },
  { value: 'no', label: 'Нет' },
];

const ORDER_OPTIONS = [
  { value: 'done', label: 'Да' },
  { value: 'no', label: 'Нет' },
];

export const DUE_LABELS = { done: 'выполнено', partial: 'частично', no: 'не выполнено' };
export const ORDER_LABELS = { done: 'да', no: 'нет' };

// Mirrors the backend's control-char rule so a stray paste is caught
// before the request, not as a bare 400.
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\x00-\x08\x0b-\x1f\x7f]/;

// ---------- «Сегодня» ----------

function RadioGroup({ name, legend, legendHint, options, value, onChange, disabled, compact, children }) {
  // The hint carries what the group is about (the order's or the due
  // action's own text), so it is the group's accessible description --
  // otherwise several «Договорённость» groups would all sound the same.
  const hintId = `${name}-hint`;
  return html`
    <fieldset
      class="radio-group${compact ? ' radio-group-compact' : ''}"
      disabled=${disabled}
      aria-describedby=${legendHint ? hintId : undefined}
    >
      <legend class="radio-legend">${legend}</legend>
      ${legendHint ? html`<p id=${hintId} class="field-hint radio-hint">${legendHint}</p>` : null}
      <div class="radio-row">
        ${options.map(
          (opt) => html`
            <label key=${String(opt.value)} class="radio-option">
              <input
                type="radio"
                class="radio-input"
                name=${name}
                value=${String(opt.value)}
                checked=${value === opt.value}
                aria-label=${opt.ariaLabel}
                onChange=${() => onChange(opt.value)}
              />
              <span class="radio-face">${opt.label}</span>
            </label>
          `,
        )}
      </div>
      ${children}
    </fieldset>
  `;
}

// Planner-style rating scale: the 1-5 segments, anchor words under the
// ends and the middle, then a live caption («4 · Хорошо»). Each radio's
// accessible name carries its word too, not only the digit.
const RATING_WORDS = { 1: 'Плохо', 2: 'Так себе', 3: 'Нормально', 4: 'Хорошо', 5: 'Отлично' };

function RatingScale({ value, onChange, disabled }) {
  return html`
    <${RadioGroup}
      name="checkin-rating"
      legend="Как прошёл день?"
      options=${RATINGS.map((r) => ({ value: r, label: String(r), ariaLabel: `${r} — ${RATING_WORDS[r]}` }))}
      value=${value}
      onChange=${onChange}
      disabled=${disabled}
      compact=${true}
    >
      <div class="rating-anchors" aria-hidden="true">
        <span>${RATING_WORDS[1]}</span>
        <span>${RATING_WORDS[3]}</span>
        <span>${RATING_WORDS[5]}</span>
      </div>
      <p class="rating-caption" aria-live="polite">
        ${value ? html`<span class="mono">${value}</span> · ${RATING_WORDS[value]}` : null}
      </p>
    <//>
  `;
}

function CheckinForm({ form, today, onSubmit, onCancel }) {
  const noteMax = (form && form.note_max) || NOTE_MAX_DEFAULT;
  const [rating, setRating] = useState(today && today.rating ? today.rating : null);
  const [due, setDue] = useState(
    today && today.due_result && today.due_result !== 'none' ? today.due_result : null,
  );
  const [orderResults, setOrderResults] = useState({});
  const [note, setNote] = useState(today && today.note ? today.note : '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const orders = (form && form.orders) || [];
  const dueAction = form ? form.due_action : null;
  const trimmed = note.trim();

  function validate() {
    if (!rating) return detailText('rating');
    if (dueAction && !due) return detailText('due_result');
    if (orders.some((o) => !orderResults[o.id])) return 'Ответь по каждому поручению.';
    if (trimmed.length > noteMax) return detailText('note_too_long');
    if (trimmed.startsWith('/')) return detailText('note_command');
    if (CONTROL_RE.test(note)) return 'В заметке есть недопустимые символы.';
    return '';
  }

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    const problem = validate();
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError('');
    const result = await onSubmit({
      rating,
      due_result: dueAction ? due : null,
      orders: orders.map((o) => ({ id: o.id, result: orderResults[o.id] })),
      note: trimmed ? trimmed : null,
    });
    setBusy(false);
    if (result !== true) setError(result);
  }

  return html`
    <form class="checkin-form" onSubmit=${submit} noValidate>
      <${RatingScale} value=${rating} onChange=${setRating} disabled=${busy} />
      ${dueAction
        ? html`
            <${RadioGroup}
              name="checkin-due"
              legend="Действие на сегодня"
              legendHint=${dueAction}
              options=${DUE_OPTIONS}
              value=${due}
              onChange=${setDue}
              disabled=${busy}
            />
          `
        : null}
      ${orders.map(
        (o) => html`
          <${RadioGroup}
            key=${o.id}
            name=${`checkin-order-${o.id}`}
            legend="Договорённость"
            legendHint=${o.text}
            options=${ORDER_OPTIONS}
            value=${orderResults[o.id] || null}
            onChange=${(v) => setOrderResults((prev) => ({ ...prev, [o.id]: v }))}
            disabled=${busy}
          />
        `,
      )}
      <div class="checkin-note">
        <label for="checkin-note" class="radio-legend">Заметка <span class="field-hint">(необязательно)</span></label>
        <textarea
          id="checkin-note"
          class="field-edit"
          rows="3"
          maxlength=${noteMax}
          disabled=${busy}
          aria-describedby="checkin-note-counter"
          value=${note}
          onInput=${(e) => setNote(e.target.value)}
        ></textarea>
        <${CharCounter} id="checkin-note-counter" length=${trimmed.length} max=${noteMax} />
      </div>
      <p class="inline-error" role="alert">${error}</p>
      <div class="card-footer">
        <span class="field-hint">Echo ответит в чате</span>
        <div class="card-footer-actions">
          ${onCancel
            ? html`<button type="button" class="btn btn-ghost" disabled=${busy} onClick=${onCancel}>Отмена</button>`
            : null}
          <button type="submit" class="btn btn-primary" disabled=${busy} aria-busy=${busy ? 'true' : 'false'}>
            ${busy ? 'Отправляю…' : 'Отправить'}
          </button>
        </div>
      </div>
    </form>
  `;
}

function CheckinSummary({ checkin }) {
  if (!checkin) return null;
  return html`
    <dl class="checkin-summary">
      <div class="summary-row">
        <dt>Оценка</dt>
        <dd>${checkin.rating ? html`<span class="mono">${checkin.rating}</span> из 5` : '—'}</dd>
      </div>
      ${checkin.due_result && checkin.due_result !== 'none'
        ? html`
            <div class="summary-row">
              <dt>Действие</dt>
              <dd>${DUE_LABELS[checkin.due_result] || checkin.due_result}</dd>
            </div>
          `
        : null}
      ${(checkin.orders || []).map(
        (o, i) => html`
          <div class="summary-row" key=${i}>
            <dt>${o.text}</dt>
            <dd>${ORDER_LABELS[o.result] || o.result}</dd>
          </div>
        `,
      )}
      ${checkin.note
        ? html`
            <div class="summary-row summary-row-note">
              <dt>Заметка</dt>
              <dd>${checkin.note}</dd>
            </div>
          `
        : null}
    </dl>
  `;
}

export function TodaySection({ data, onSubmit }) {
  const [redo, setRedo] = useState(false);
  const redoButtonRef = useRef(null);
  const headingRef = useRef(null);

  const wasRedoRef = useRef(false);

  // A finished submit (or a check-in completed from Telegram) closes
  // the redo form.
  useEffect(() => {
    if (data.in_progress) setRedo(false);
  }, [data.in_progress]);

  // «Пройти заново» unmounts itself, and «Отмена» unmounts the form:
  // either way focus moves somewhere meaningful instead of dropping to
  // <body> -- the section heading when the redo form opens, back to
  // «Пройти заново» when it is cancelled.
  useEffect(() => {
    if (redo && !wasRedoRef.current && headingRef.current) headingRef.current.focus();
    if (!redo && wasRedoRef.current && redoButtonRef.current) redoButtonRef.current.focus();
    wasRedoRef.current = redo;
  }, [redo]);

  // done_today follows last_checkin_at, which a restarted check-in (a
  // /checkin typed again after finishing) does not reset -- but the
  // restart does clear today's answers. An unrated row today is a
  // check-in under way, not a finished one.
  const restarted = data.done_today && data.today && data.today.rating == null;
  const doneToday = data.done_today && !restarted;

  const streakText = html`Стрик: <span class="mono">${data.streak}</span> ${pluralRu(data.streak, ['день', 'дня', 'дней'])}`;

  let body;
  let statusText = '';
  if (data.in_progress) {
    statusText = 'Отправлено — Echo ответит в чате.';
    body = html`
      <p class="field-value">Отправлено — Echo ответит в чате.</p>
      <a class="btn chat-link" href="#/chat">Открыть чат</a>
    `;
  } else if (doneToday && !redo) {
    statusText = 'Чек-ин на сегодня пройден.';
    body = html`
      <p class="field-value">Чек-ин на сегодня пройден.</p>
      <${CheckinSummary} checkin=${data.today} />
      <div class="btn-row">
        <button type="button" class="btn" ref=${redoButtonRef} onClick=${() => setRedo(true)}>Пройти заново</button>
      </div>
    `;
  } else {
    // The DTO does not say where an unfinished check-in was started
    // (Telegram, the web chat's /checkin, or an earlier web submit), so
    // the hint does not name a source.
    const startedElsewhere = !doneToday && data.today && data.today.rating;
    body = html`
      ${startedElsewhere || restarted
        ? html`<p class="field-hint">Чек-ин начат, но не завершён — можно закончить здесь.</p>`
        : null}
      <${CheckinForm}
        key=${`${data.local_date}|${redo ? 'redo' : 'new'}`}
        form=${data.form}
        today=${redo || startedElsewhere ? data.today : null}
        onSubmit=${async (body) => {
          const result = await onSubmit(body);
          if (result === true) {
            setRedo(false);
            if (headingRef.current) headingRef.current.focus();
          }
          return result;
        }}
        onCancel=${redo ? () => setRedo(false) : null}
      />
    `;
  }

  return html`
    <section class="card" aria-labelledby="checkin-today-heading">
      <div class="card-row">
        <h2 id="checkin-today-heading" tabindex="-1" ref=${headingRef}>Чек-ин</h2>
        <span class="field-hint">${streakText}</span>
      </div>
      <p class="sr-only" role="status">${statusText}</p>
      ${body}
    </section>
  `;
}

// ---------- data ----------

// While a submitted check-in is still being processed («Отправлено»),
// the section rechecks this often. The worker's finish publishes
// invalidate("checkin"), so this only matters if that event was lost.
const IN_PROGRESS_POLL_MS = 10000;

// GET/POST /api/checkin for Сегодня: {data, failed, reload, submit}.
// A submit is not applied locally -- POST returns 202 {} and the
// reaction runs in the worker (the reply lands in the web chat) -- so
// the section only ever shows what the next GET returns.
export function useCheckinToday() {
  const main = useResource('/api/checkin', 'checkin');
  const inProgress = !!(main.data && main.data.in_progress);

  useEffect(() => {
    if (!inProgress) return undefined;
    const timer = setInterval(main.reload, IN_PROGRESS_POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line
  }, [inProgress]);

  async function submit(body) {
    const res = await send('/api/checkin', body, { fallback: 'Не удалось отправить.' });
    if (res.status === 202) {
      pushToast('Отправлено.');
      // Only this section changes now (to «Отправлено»); Дневник's chart
      // and journal follow with the invalidate the worker publishes.
      await main.reload();
      return true;
    }
    if (res.status === 422) {
      const detail = res.data && res.data.detail;
      if (detail === 'orders' || detail === 'due_result') await main.reload();
      return res.error;
    }
    if (res.status === 409) {
      await main.reload();
      return 'Предыдущий чек-ин ещё обрабатывается — ответ придёт в чат.';
    }
    if (res.status === 400) return 'Не удалось отправить: неверные данные.';
    return res.error;
  }

  return { data: main.data, failed: main.failed, reload: main.reload, submit };
}
