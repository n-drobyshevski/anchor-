// The check-in screen (#/checkin, nav label "Чек-ин"): the W4 HTTP
// contract's GET/POST /api/checkin, GET /api/checkins?days=30 and
// GET /api/journal. Three sections:
//
//   «Сегодня»  - the check-in form, or a summary of today's finished
//                check-in with «Пройти заново», or «Отправлено» while
//                a web-submitted one is still waiting for the worker.
//   «30 дней»  - a single-series inline-SVG bar chart of daily ratings
//                plus the history list, which is the chart's table view
//                (every value the tooltip shows is also readable there).
//   «Журнал»   - journal lines grouped by local day, paged with
//                «Показать ещё» exactly like Memory.js.
//
// A submit is not applied locally: POST returns 202 {} and the reaction
// runs in the worker (the reply lands in the web chat via SSE), so this
// screen only ever shows what the next GET returns -- the same
// optimistic-free rule State.js follows for pause.
//
// Every "YYYY-MM-DD" is parsed by hand into UTC-midnight arithmetic,
// never through the Date string parser: `new Date('2026-09-23')` is
// UTC midnight, which displays as the previous day anywhere west of
// Greenwich.
import { html } from '../html.js';
import { useEffect, useLayoutEffect, useRef, useState } from '../../vendor/hooks.module.js';
import { apiGet, apiPost } from '../api.js';
import { forceLogout, pushToast } from '../store.js';
import { useAutoRefetch } from '../hooks.js';
import { Toasts } from '../ui/Toasts.js';

const CHART_DAYS = 30;
const JOURNAL_PAGE = 30;
// GET /api/journal's own cap (contract: limit max 50).
const JOURNAL_MAX_LIMIT = 50;
// Fallback only -- the form DTO carries the real cap as `note_max`.
const NOTE_MAX_DEFAULT = 500;

const RATINGS = [1, 2, 3, 4, 5];

const DUE_OPTIONS = [
  { value: 'done', label: 'Да' },
  { value: 'partial', label: 'Частично' },
  { value: 'no', label: 'Нет' },
];

const ORDER_OPTIONS = [
  { value: 'done', label: 'Да' },
  { value: 'no', label: 'Нет' },
];

const DUE_LABELS = { done: 'выполнено', partial: 'частично', no: 'не выполнено' };
const ORDER_LABELS = { done: 'да', no: 'нет' };

// 422 `detail` -> Russian, per the contract's list.
const DETAIL_MESSAGES = {
  rating: 'Выбери оценку от 1 до 5.',
  note_too_long: 'Заметка слишком длинная.',
  note_command: 'Заметка не может начинаться с «/».',
  due_result: 'Отметь, как прошло действие на сегодня.',
  orders: 'Список поручений изменился — форма обновлена.',
};

const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTHS_GENITIVE = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
];
const WEEKDAYS = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
const WEEKDAYS_SHORT = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

// ---------- helpers ----------

// Russian plural: pluralRu(5, ['день', 'дня', 'дней']) -> 'дней'.
export function pluralRu(n, forms) {
  const abs = Math.abs(n) % 100;
  const last = abs % 10;
  if (abs > 10 && abs < 20) return forms[2];
  if (last === 1) return forms[0];
  if (last >= 2 && last <= 4) return forms[1];
  return forms[2];
}

// "YYYY-MM-DD" -> UTC-midnight milliseconds, or NaN if malformed.
function parseLocalDate(s) {
  if (typeof s !== 'string' || s.length !== 10 || s[4] !== '-' || s[7] !== '-') return NaN;
  const y = Number(s.slice(0, 4));
  const m = Number(s.slice(5, 7));
  const d = Number(s.slice(8, 10));
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return NaN;
  if (m < 1 || m > 12 || d < 1 || d > 31) return NaN;
  return Date.UTC(y, m - 1, d);
}

function dateKey(ms) {
  const dt = new Date(ms);
  const y = dt.getUTCFullYear();
  const m = String(dt.getUTCMonth() + 1).padStart(2, '0');
  const d = String(dt.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

const DAY_MS = 86400000;

function shortDate(ms) {
  const dt = new Date(ms);
  return `${dt.getUTCDate()} ${MONTHS_SHORT[dt.getUTCMonth()]}`;
}

function shortDateWithWeekday(ms) {
  const dt = new Date(ms);
  return `${WEEKDAYS_SHORT[dt.getUTCDay()]}, ${dt.getUTCDate()} ${MONTHS_SHORT[dt.getUTCMonth()]}`;
}

// «Сегодня» / «Вчера» / «23 сентября, среда», relative to `todayKey`.
function dayHeading(key, todayKey) {
  const ms = parseLocalDate(key);
  if (Number.isNaN(ms)) return key;
  const todayMs = parseLocalDate(todayKey);
  if (!Number.isNaN(todayMs)) {
    if (ms === todayMs) return 'Сегодня';
    if (ms === todayMs - DAY_MS) return 'Вчера';
  }
  const dt = new Date(ms);
  const todayYear = Number.isNaN(todayMs) ? null : new Date(todayMs).getUTCFullYear();
  const year = todayYear !== null && todayYear !== dt.getUTCFullYear() ? ` ${dt.getUTCFullYear()}` : '';
  return `${dt.getUTCDate()} ${MONTHS_GENITIVE[dt.getUTCMonth()]}${year}, ${WEEKDAYS[dt.getUTCDay()]}`;
}

function retryText(retryAfterSeconds) {
  const s = Number(retryAfterSeconds) || 60;
  if (s < 60) {
    const n = Math.max(1, Math.ceil(s));
    return `Слишком много попыток — попробуй через ${n} ${pluralRu(n, ['секунду', 'секунды', 'секунд'])}.`;
  }
  const m = Math.ceil(s / 60);
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

// Mirrors the backend's control-char rule so a stray paste is caught
// before the request, not as a bare 400.
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\x00-\x08\x0b-\x1f\x7f]/;

// ---------- «Сегодня» ----------

function RadioGroup({ name, legend, legendHint, options, value, onChange, disabled, compact }) {
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
                onChange=${() => onChange(opt.value)}
              />
              <span class="radio-face">${opt.label}</span>
            </label>
          `,
        )}
      </div>
    </fieldset>
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
    if (!rating) return DETAIL_MESSAGES.rating;
    if (dueAction && !due) return DETAIL_MESSAGES.due_result;
    if (orders.some((o) => !orderResults[o.id])) return 'Ответь по каждому поручению.';
    if (trimmed.length > noteMax) return DETAIL_MESSAGES.note_too_long;
    if (trimmed.startsWith('/')) return DETAIL_MESSAGES.note_command;
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
      <${RadioGroup}
        name="checkin-rating"
        legend="Как прошёл день?"
        legendHint="1 — плохо, 5 — отлично"
        options=${RATINGS.map((r) => ({ value: r, label: String(r) }))}
        value=${rating}
        onChange=${setRating}
        disabled=${busy}
        compact=${true}
      />
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
        <p id="checkin-note-counter" class="char-counter${trimmed.length > noteMax ? ' char-counter-over' : ''}">
          ${trimmed.length}/${noteMax}
        </p>
      </div>
      <div class="btn-row">
        <button type="submit" class="btn btn-primary" disabled=${busy} aria-busy=${busy ? 'true' : 'false'}>
          ${busy ? 'Отправляю…' : 'Отправить'}
        </button>
        ${onCancel
          ? html`<button type="button" class="btn btn-ghost" disabled=${busy} onClick=${onCancel}>Отмена</button>`
          : null}
      </div>
      <p class="inline-error" role="alert">${error}</p>
    </form>
  `;
}

function CheckinSummary({ checkin }) {
  if (!checkin) return null;
  return html`
    <dl class="checkin-summary">
      <div class="summary-row">
        <dt>Оценка</dt>
        <dd>${checkin.rating ? `${checkin.rating} из 5` : '—'}</dd>
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

function TodaySection({ data, onSubmit }) {
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

  const streakText = `Стрик: ${data.streak} ${pluralRu(data.streak, ['день', 'дня', 'дней'])}`;

  let body;
  let statusText = '';
  if (data.in_progress) {
    statusText = 'Отправлено — Anchor ответит в чате.';
    body = html`
      <p class="field-value">Отправлено — Anchor ответит в чате.</p>
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
        <h2 id="checkin-today-heading" tabindex="-1" ref=${headingRef}>Сегодня</h2>
        <span class="field-hint">${streakText}</span>
      </div>
      <p class="sr-only" role="status">${statusText}</p>
      ${body}
    </section>
  `;
}

// ---------- «30 дней» chart ----------

// Numeric layout constants (px). The SVG's width is the measured card
// width, so every coordinate below is a real pixel, and text never
// scales with the viewport the way a fixed viewBox would.
const CH = {
  height: 168,
  top: 10,
  bottom: 22, // x-axis label band, inside the SVG's own height
  left: 18, // y tick labels
  right: 4,
  maxBar: 16,
  minBar: 3,
  radius: 4,
};

// A column path: square at the baseline, rounded at the data end.
// Built only from numbers.
function barPath(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  const f = (n) => Math.round(n * 100) / 100;
  return (
    `M${f(x)} ${f(y + h)}` +
    `V${f(y + rr)}` +
    `Q${f(x)} ${f(y)} ${f(x + rr)} ${f(y)}` +
    `H${f(x + w - rr)}` +
    `Q${f(x + w)} ${f(y)} ${f(x + w)} ${f(y + rr)}` +
    `V${f(y + h)}Z`
  );
}

function useElementWidth(fallback) {
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

function RatingChart({ days, endKey }) {
  const [wrapRef, width] = useElementWidth(320);
  const [active, setActive] = useState(null); // index into `slots`, or null
  const [announce, setAnnounce] = useState('');
  const tipRef = useRef(null);
  // A tap both presses and focuses the SVG; the focus handler must keep
  // the tapped day rather than jumping to its keyboard starting point.
  const pointerIdxRef = useRef(null);

  const byDate = new Map(days.map((c) => [c.local_date, c]));
  const endMs = parseLocalDate(endKey);
  const slots = [];
  if (!Number.isNaN(endMs)) {
    for (let i = CHART_DAYS - 1; i >= 0; i -= 1) {
      const ms = endMs - i * DAY_MS;
      slots.push({ ms, item: byDate.get(dateKey(ms)) || null });
    }
  }

  const plotW = Math.max(60, width - CH.left - CH.right);
  const plotH = CH.height - CH.top - CH.bottom;
  const band = plotW / Math.max(1, slots.length);
  const barW = Math.max(CH.minBar, Math.min(CH.maxBar, band - 3));
  const baseY = CH.top + plotH;
  const yFor = (v) => baseY - (v / 5) * plotH;
  const centerX = (i) => CH.left + band * i + band / 2;

  const rated = slots.filter((s) => s.item && s.item.rating);
  const avg = rated.length ? rated.reduce((sum, s) => sum + s.item.rating, 0) / rated.length : 0;
  const summary = rated.length
    ? `Оценки дня за 30 дней: ${rated.length} ${pluralRu(rated.length, ['день', 'дня', 'дней'])} с оценкой, `
      + `средняя ${avg.toFixed(1).replace('.', ',')}. Подробно — в списке ниже.`
    : 'Оценки дня за 30 дней: пока нет ни одной.';

  // Sparse x labels: the last day, then every 7th back from it.
  const labelIdx = [];
  for (let i = slots.length - 1; i >= 0; i -= 7) labelIdx.push(i);

  function tipText(i) {
    const s = slots[i];
    if (!s) return { value: '', label: '' };
    const label = shortDateWithWeekday(s.ms);
    if (!s.item || !s.item.rating) return { value: 'нет оценки', label };
    return { value: `${s.item.rating} из 5`, label };
  }

  // Position the tooltip over the active bar via CSSOM (never a style
  // attribute string), clamped inside the chart box.
  useLayoutEffect(() => {
    const tip = tipRef.current;
    if (!tip || active === null) return;
    const tipW = tip.offsetWidth || 0;
    const x = centerX(active) - tipW / 2;
    const clamped = Math.max(0, Math.min(width - tipW, x));
    tip.style.left = `${Math.round(clamped)}px`;
  });

  function indexFromPointer(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left - CH.left;
    const i = Math.floor(x / band);
    return Math.max(0, Math.min(slots.length - 1, i));
  }

  function onPointerMove(e) {
    if (!slots.length) return;
    setActive(indexFromPointer(e));
  }

  function onPointerDown(e) {
    if (!slots.length) return;
    const i = indexFromPointer(e);
    pointerIdxRef.current = i;
    setActive(i);
  }

  function onPointerLeave(e) {
    if (document.activeElement !== e.currentTarget) setActive(null);
  }

  function moveTo(i) {
    const next = Math.max(0, Math.min(slots.length - 1, i));
    setActive(next);
    const t = tipText(next);
    setAnnounce(`${t.label}: ${t.value}`);
  }

  function onFocus() {
    if (!slots.length) return;
    if (pointerIdxRef.current !== null) {
      setActive(pointerIdxRef.current);
      pointerIdxRef.current = null;
      return;
    }
    // Start on the most recent rated day, else the last slot.
    let start = slots.length - 1;
    for (let i = slots.length - 1; i >= 0; i -= 1) {
      if (slots[i].item && slots[i].item.rating) {
        start = i;
        break;
      }
    }
    moveTo(active === null ? start : active);
  }

  function onKeyDown(e) {
    if (!slots.length) return;
    const cur = active === null ? slots.length - 1 : active;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
      e.preventDefault();
      moveTo(cur - 1);
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
      e.preventDefault();
      moveTo(cur + 1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      moveTo(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      moveTo(slots.length - 1);
    } else if (e.key === 'Escape') {
      setActive(null);
    }
  }

  const tip = active !== null ? tipText(active) : null;

  return html`
    <div class="chart" ref=${wrapRef}>
      <svg
        class="chart-svg"
        width=${width}
        height=${CH.height}
        viewBox=${`0 0 ${width} ${CH.height}`}
        role="img"
        aria-label=${summary}
        aria-describedby="checkin-chart-hint"
        tabindex="0"
        onPointerMove=${onPointerMove}
        onPointerDown=${onPointerDown}
        onPointerLeave=${onPointerLeave}
        onFocus=${onFocus}
        onBlur=${() => setActive(null)}
        onKeyDown=${onKeyDown}
      >
        ${RATINGS.map(
          (v) => html`
            <line
              key=${`g${v}`}
              class="chart-grid"
              x1=${CH.left}
              x2=${CH.left + plotW}
              y1=${yFor(v)}
              y2=${yFor(v)}
            />
            <text key=${`t${v}`} class="chart-tick" x=${CH.left - 6} y=${yFor(v) + 4} text-anchor="end">${v}</text>
          `,
        )}
        <line class="chart-axis" x1=${CH.left} x2=${CH.left + plotW} y1=${baseY} y2=${baseY} />
        ${slots.map((s, i) => {
          const x = centerX(i) - barW / 2;
          const isActive = active === i;
          if (s.item && s.item.rating) {
            const y = yFor(s.item.rating);
            return html`
              <path
                key=${`b${i}`}
                class=${`chart-bar${isActive ? ' is-active' : ''}`}
                d=${barPath(x, y, barW, baseY - y, CH.radius)}
              />
            `;
          }
          return html`
            <rect
              key=${`e${i}`}
              class=${`chart-empty${isActive ? ' is-active' : ''}`}
              x=${x}
              y=${baseY - 2}
              width=${barW}
              height=${2}
            />
          `;
        })}
        ${active !== null
          ? html`
              <line
                class="chart-cursor"
                x1=${centerX(active)}
                x2=${centerX(active)}
                y1=${CH.top}
                y2=${baseY}
              />
            `
          : null}
        ${labelIdx.map((i) => {
          const last = i === slots.length - 1;
          return html`
            <text
              key=${`x${i}`}
              class="chart-tick"
              x=${last ? CH.left + plotW : centerX(i)}
              y=${CH.height - 6}
              text-anchor=${last ? 'end' : 'middle'}
            >
              ${shortDate(slots[i].ms)}
            </text>
          `;
        })}
      </svg>
      <div class="chart-tip" ref=${tipRef} hidden=${!tip} aria-hidden="true">
        ${tip ? html`<strong class="chart-tip-value">${tip.value}</strong><span class="chart-tip-label">${tip.label}</span>` : null}
      </div>
      <p id="checkin-chart-hint" class="sr-only">Стрелки влево и вправо — перейти между днями.</p>
      <p class="sr-only" aria-live="polite">${announce}</p>
    </div>
  `;
}

function HistoryList({ items }) {
  const rows = items.slice().reverse();
  if (!rows.length) return html`<p class="field-hint">Пока нет чек-инов.</p>`;
  return html`
    <ul class="history-list checkin-history">
      ${rows.map((c) => {
        const ms = parseLocalDate(c.local_date);
        const details = [];
        if (c.due_result && c.due_result !== 'none') details.push(`действие: ${DUE_LABELS[c.due_result] || c.due_result}`);
        for (const o of c.orders || []) details.push(`${o.text}: ${ORDER_LABELS[o.result] || o.result}`);
        return html`
          <li class="history-row" key=${c.local_date}>
            <div class="history-main">
              <p class="field-value">${Number.isNaN(ms) ? c.local_date : shortDateWithWeekday(ms)}</p>
              ${details.length ? html`<p class="field-hint">${details.join(' · ')}</p>` : null}
              ${c.note ? html`<p class="field-hint checkin-note-text">${c.note}</p>` : null}
            </div>
            <span class="status-chip rating-chip" data-rating=${c.rating ? String(c.rating) : 'none'}>
              ${c.rating ? `${c.rating}/5` : '—'}
            </span>
          </li>
        `;
      })}
    </ul>
  `;
}

function MonthSection({ range, failed, onRetry }) {
  return html`
    <section class="card" aria-labelledby="checkin-range-heading">
      <h2 id="checkin-range-heading">30 дней</h2>
      ${failed && !range
        ? html`
            <p class="field-hint">
              Не удалось загрузить.
              <button type="button" class="link-button" onClick=${onRetry}>Повторить</button>
            </p>
          `
        : !range
          ? html`<div aria-busy="true" class="chart-placeholder"></div>`
          : html`
              <p class="field-hint">Оценка дня, 1–5</p>
              <${RatingChart} days=${range.items} endKey=${range.to} />
              <h3 class="subheading">История</h3>
              <${HistoryList} items=${range.items} />
            `}
    </section>
  `;
}

// ---------- «Журнал» ----------

// A journal line's time of day, in the browser's zone (like Chat's
// message times).
function hmLocal(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function groupByDay(items) {
  const groups = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.key === item.local_date) last.items.push(item);
    else groups.push({ key: item.local_date, items: [item] });
  }
  return groups;
}

function JournalSection({ items, total, loaded, failed, loadingMore, todayKey, onMore, onRetry }) {
  const groups = groupByDay(items);
  const hasMore = items.length < total;
  return html`
    <section class="card" aria-labelledby="checkin-journal-heading">
      <div class="card-row">
        <h2 id="checkin-journal-heading">Журнал</h2>
        ${loaded ? html`<span class="field-hint">${total} ${pluralRu(total, ['запись', 'записи', 'записей'])}</span>` : null}
      </div>
      ${failed && !loaded
        ? html`
            <p class="field-hint">
              Не удалось загрузить.
              <button type="button" class="link-button" onClick=${onRetry}>Повторить</button>
            </p>
          `
        : !loaded
          ? html`<div aria-busy="true"></div>`
          : groups.length
            ? html`
                <div class="journal">
                  ${groups.map(
                    (g) => html`
                      <div class="journal-day" key=${g.key}>
                        <h3 class="subheading">${dayHeading(g.key, todayKey)}</h3>
                        <ul class="journal-list">
                          ${g.items.map(
                            (it) => html`
                              <li key=${it.id} class="journal-item">
                                ${it.created_at
                                  ? html`<span class="journal-time mono">${hmLocal(it.created_at)}</span>`
                                  : null}
                                <span>${it.text}</span>
                              </li>
                            `,
                          )}
                        </ul>
                      </div>
                    `,
                  )}
                </div>
              `
            : html`<p class="field-hint">Журнал пока пуст — Anchor добавляет сюда заметки из разговоров.</p>`}
      ${hasMore
        ? html`
            <div class="btn-row">
              <button type="button" class="btn" disabled=${loadingMore} onClick=${onMore}>Показать ещё</button>
            </div>
          `
        : null}
    </section>
  `;
}

// ---------- screen ----------

export function Checkin() {
  const [data, setData] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [range, setRange] = useState(null);
  const [rangeFailed, setRangeFailed] = useState(false);
  const [journal, setJournal] = useState([]);
  const [journalTotal, setJournalTotal] = useState(0);
  const [journalLoaded, setJournalLoaded] = useState(false);
  const [journalFailed, setJournalFailed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Same stale-response guard as Memory.js: a replacing fetch claims a
  // new number; an append (loadMore) is dropped if one moved past it.
  const journalSeq = useRef(0);
  const mainSeq = useRef(0);
  const rangeSeq = useRef(0);

  async function loadMain() {
    const seq = ++mainSeq.current;
    const res = await apiGet('/api/checkin');
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (seq !== mainSeq.current) return;
    if (res.ok && res.data) {
      setData(res.data);
      setLoadFailed(false);
    } else {
      setLoadFailed(true);
    }
  }

  async function loadRange() {
    const seq = ++rangeSeq.current;
    const res = await apiGet(`/api/checkins?days=${CHART_DAYS}`);
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (seq !== rangeSeq.current) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) {
      setRange(res.data);
      setRangeFailed(false);
    } else {
      setRangeFailed(true);
    }
  }

  function journalQuery(offset, limit) {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return `/api/journal?${params.toString()}`;
  }

  // Refetches however many journal rows are already loaded (in chunks
  // of the endpoint's cap), so a background refresh does not undo a
  // «Показать ещё» -- same reasoning as Memory.js's reload().
  async function loadJournal() {
    const seq = ++journalSeq.current;
    const target = Math.max(journal.length, JOURNAL_PAGE);
    const collected = [];
    let last = null;
    for (let offset = 0; offset < target; offset += JOURNAL_MAX_LIMIT) {
      const chunk = Math.min(JOURNAL_MAX_LIMIT, target - offset);
      const res = await apiGet(journalQuery(offset, chunk));
      if (res.status === 401) {
        forceLogout();
        return;
      }
      if (seq !== journalSeq.current) return;
      if (!res.ok || !res.data || !Array.isArray(res.data.items)) {
        setJournalFailed(true);
        return;
      }
      collected.push(...res.data.items);
      last = res.data;
      if (res.data.items.length < chunk) break;
    }
    if (!last) return;
    setJournal(collected);
    setJournalTotal(last.total);
    setJournalLoaded(true);
    setJournalFailed(false);
  }

  async function loadMoreJournal() {
    const seq = journalSeq.current;
    setLoadingMore(true);
    const res = await apiGet(journalQuery(journal.length, JOURNAL_PAGE));
    setLoadingMore(false);
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (seq !== journalSeq.current) return;
    if (res.ok && res.data && Array.isArray(res.data.items)) {
      // De-duplicate by id: a row written between pages shifts offsets.
      setJournal((prev) => {
        const seen = new Set(prev.map((it) => it.id));
        return [...prev, ...res.data.items.filter((it) => !seen.has(it.id))];
      });
      setJournalTotal(res.data.total);
    } else {
      pushToast('Не удалось загрузить.');
    }
  }

  function reload() {
    return Promise.all([loadMain(), loadRange(), loadJournal()]);
  }

  // 'checkin' covers check-in rows, journal rows (tail maps the journal
  // field to it in W4) and this screen's own POST.
  useAutoRefetch('checkin', reload);

  async function submit(body) {
    const res = await apiPost('/api/checkin', body);
    if (res.status === 401) {
      forceLogout();
      return 'Сессия истекла.';
    }
    if (res.status === 202) {
      pushToast('Отправлено.');
      await reload();
      return true;
    }
    if (res.status === 422) {
      const detail = res.data && res.data.detail;
      if (detail === 'orders' || detail === 'due_result') await loadMain();
      return DETAIL_MESSAGES[detail] || 'Неверное значение.';
    }
    if (res.status === 409) {
      await loadMain();
      return 'Предыдущий чек-ин ещё обрабатывается — ответ придёт в чат.';
    }
    if (res.status === 429) {
      return retryText(res.data && res.data.retry_after);
    }
    if (res.status === 400) return 'Не удалось отправить: неверные данные.';
    return 'Не удалось отправить.';
  }

  if (loadFailed && !data) {
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
    <div class="screen-wrap screen-wrap-wide">
      <div class="screen screen-checkin">
        <div class="checkin-main">
          <${TodaySection} data=${data} onSubmit=${submit} />
        </div>
        <div class="checkin-side">
          <${MonthSection} range=${range} failed=${rangeFailed} onRetry=${loadRange} />
          <${JournalSection}
            items=${journal}
            total=${journalTotal}
            loaded=${journalLoaded}
            failed=${journalFailed}
            loadingMore=${loadingMore}
            todayKey=${data.local_date}
            onMore=${loadMoreJournal}
            onRetry=${loadJournal}
          />
        </div>
      </div>
      <${Toasts} />
    </div>
  `;
}
