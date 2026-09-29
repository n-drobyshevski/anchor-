// Сегодня's state cards, moved out of the old State screen unchanged:
// the day's action (DueCard), Режим (focus, quiet, pause), today's
// spend and the quiet metadata line. The screen that renders them
// (screens/Today.js) owns the StateDTO and the mutations; every card
// here owns only its own edit/busy/error state.
import { html } from '../../html.js';
import { useEffect, useRef, useState } from '../../../vendor/hooks.module.js';
import { pluralRu, usd } from '../../lib/format.js';
import { dayWordInTz, formatHmInTz, formatQuietUntil, isoNextMorning, isoPlusHours, sinceText } from '../../lib/tz.js';
import { useInlineEdit } from '../../ui/inlineEdit.js';

// ---------- Действие на сегодня ----------

export function DueCard({ due, dueMaxLen, timezone, onSave }) {
  const {
    editing, draft, setDraft, error, busy, textareaRef, openerRef,
    start: startEdit, cancel: cancelEdit, save, onKeyDown,
  } = useInlineEdit(due.action || '', onSave);

  return html`
    <section class="card" aria-labelledby="due-heading">
      <h2 id="due-heading" class="field-label">Действие на сегодня</h2>
      ${editing
        ? html`
            <textarea
              ref=${textareaRef}
              aria-labelledby="due-heading"
              class="field-edit"
              rows="3"
              maxlength=${dueMaxLen}
              disabled=${busy}
              value=${draft}
              onInput=${(e) => setDraft(e.target.value)}
              onKeyDown=${onKeyDown}
            ></textarea>
            ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
            <div class="card-footer">
              <span></span>
              <div class="card-footer-actions">
                <button type="button" class="btn btn-ghost" disabled=${busy} onClick=${cancelEdit}>Отмена</button>
                <button type="button" class="btn btn-primary" disabled=${busy} onClick=${save}>Сохранить</button>
              </div>
            </div>
          `
        : html`
            <p class="field-value due-value">${due.action || 'Не задано'}</p>
            <div class="card-footer">
              ${due.action && due.set_at
                ? html`<span class="field-hint">задано в <span class="mono">${formatHmInTz(due.set_at, timezone)}</span></span>`
                : html`<span></span>`}
              <button
                type="button"
                ref=${openerRef}
                class="btn"
                onClick=${startEdit}
                aria-label="Изменить действие на сегодня"
              >
                Изменить
              </button>
            </div>
          `}
    </section>
  `;
}

// ---------- Режим: Фокус / Тишина / Пауза ----------

function FocusRow({ focus, timezone, onToggle }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function toggle() {
    setBusy(true);
    setError('');
    const result = await onToggle(!focus.on);
    setBusy(false);
    if (result !== true) setError(result);
  }

  return html`
    <li class="row">
      <div class="row-main">
        <span id="focus-heading" class="row-title">Фокус</span>
        ${focus.on && focus.since
          ? html`<span class="field-hint">с <span class="mono">${sinceText(focus.since, timezone)}</span></span>`
          : null}
        ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked=${focus.on ? 'true' : 'false'}
        aria-labelledby="focus-heading"
        class="switch"
        disabled=${busy}
        onClick=${toggle}
      >
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </button>
    </li>
  `;
}

function QuietRow({ quietUntil, timezone, onSet }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // The backend never clears `quiet_until` once it passes -- only the
  // worker/gate compare it with "now" when deciding whether to send --
  // so StateDTO can (and routinely does) return an already-expired
  // value; the row must not show that as still active.
  const active = !!quietUntil && Date.parse(quietUntil) > Date.now();

  async function apply(iso) {
    setBusy(true);
    setError('');
    const result = await onSet(iso);
    setBusy(false);
    if (result !== true) setError(result);
  }

  return html`
    <li class="row row-stacked">
      <div class="row-head">
        <span id="quiet-heading" class="row-title">Тишина</span>
        <span class="row-value">
          ${active
            ? html`<span class="field-hint">до <span class="mono">${formatQuietUntil(quietUntil, timezone)}</span></span>`
            : html`<span class="field-hint">выкл</span>`}
          <button
            type="button"
            class="btn btn-ghost"
            disabled=${busy || !active}
            onClick=${() => apply(null)}
          >
            Выключить
          </button>
        </span>
      </div>
      <div class="segmented" role="group" aria-labelledby="quiet-heading">
        <button type="button" disabled=${busy} onClick=${() => apply(isoPlusHours(1))}>1 ч</button>
        <button type="button" disabled=${busy} onClick=${() => apply(isoPlusHours(2))}>2 ч</button>
        <button type="button" disabled=${busy} onClick=${() => apply(isoPlusHours(4))}>4 ч</button>
        <button
          type="button"
          disabled=${busy}
          onClick=${async () => {
            try {
              await apply(isoNextMorning(timezone));
            } catch {
              setError('Не удалось вычислить время.');
            }
          }}
        >
          До утра
        </button>
      </div>
      ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
    </li>
  `;
}

// Pause has no `state` in its POST response (202 {}) -- see State()'s
// own comment on `togglePause` for why the switch's real value only
// catches up once a later `reload()` lands. Without `pendingTarget`
// below, that gap (the worker poll plus the tail's own poll interval,
// several seconds) left the switch showing its *old* value right after
// a successful tap, indistinguishable from the tap not having
// registered at all -- inviting a second tap that enqueues the same
// /out or /in a second time (a duplicate canned reply in chat).
const PENDING_CLEAR_MS = 15000;

function PauseRow({ paused, onToggle }) {
  const [busy, setBusy] = useState(false);
  // The target value a just-accepted (202) toggle is waiting to see
  // reflected in `paused`; null when nothing is in flight.
  const [pendingTarget, setPendingTarget] = useState(null);
  const timeoutRef = useRef(null);

  // `paused` catching up to what we asked for -- via reload() after
  // the tail's invalidate("state") -- clears the pending state right
  // away rather than waiting out the full timeout.
  useEffect(() => {
    if (pendingTarget !== null && paused === pendingTarget) {
      clearTimeout(timeoutRef.current);
      setPendingTarget(null);
    }
  }, [paused, pendingTarget]);

  useEffect(() => () => clearTimeout(timeoutRef.current), []);

  async function toggle() {
    const target = !paused;
    setBusy(true);
    const ok = await onToggle(target);
    setBusy(false);
    if (ok) {
      clearTimeout(timeoutRef.current);
      setPendingTarget(target);
      // A safety net, not the primary path: if the tail's invalidate
      // never arrives (a dropped SSE event) the switch still re-enables
      // on its own after a generous window, rather than staying stuck
      // "Применяется…" forever.
      timeoutRef.current = setTimeout(() => setPendingTarget(null), PENDING_CLEAR_MS);
    }
    // A non-202 response needs no explicit clearing: `ok` is false, so
    // `pendingTarget` (already null, since nothing before this call
    // could have set it while this same toggle() was in flight -- the
    // switch is disabled below for the whole call) is never set in the
    // first place.
  }

  const applying = pendingTarget !== null;

  return html`
    <li class="row">
      <div class="row-main">
        <span id="pause-heading" class="row-title">Пауза</span>
        <span class="field-hint">${applying ? 'Применяется…' : 'Echo ответит в чате'}</span>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked=${paused ? 'true' : 'false'}
        aria-busy=${applying ? 'true' : 'false'}
        aria-labelledby="pause-heading"
        class="switch"
        disabled=${busy || applying}
        onClick=${toggle}
      >
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </button>
    </li>
  `;
}

export function ModeCard({ state, onFocus, onQuiet, onPause }) {
  return html`
    <section class="card" aria-labelledby="mode-heading">
      <h2 id="mode-heading">Режим</h2>
      <ul class="card-list">
        <${FocusRow} focus=${state.focus} timezone=${state.timezone} onToggle=${onFocus} />
        <${QuietRow} quietUntil=${state.quiet_until} timezone=${state.timezone} onSet=${onQuiet} />
        <${PauseRow} paused=${state.paused} onToggle=${onPause} />
      </ul>
    </section>
  `;
}

// ---------- Траты сегодня ----------

export function SpendCard({ spend }) {
  const categories = Object.entries(spend.by_category || {}).sort((a, b) => b[1] - a[1]);
  return html`
    <section class="card" aria-labelledby="spend-heading">
      <div class="card-row">
        <h2 id="spend-heading">Траты сегодня</h2>
        <span class="mono spend-total">${usd(spend.today_usd)} / ${usd(spend.cap_usd)}</span>
      </div>
      <progress
        aria-labelledby="spend-heading"
        aria-valuetext=${`${usd(spend.today_usd)} из ${usd(spend.cap_usd)}`}
        value=${spend.today_usd}
        max=${Math.max(spend.cap_usd, spend.today_usd, 0.01)}
      ></progress>
      ${categories.length
        ? html`
            <ul class="category-list">
              ${categories.map(
                ([name, amount]) => html`
                  <li key=${name}><span>${name}</span><span class="mono">${usd(amount)}</span></li>
                `,
              )}
            </ul>
          `
        : null}
    </section>
  `;
}

// ---------- metadata line ----------
// The streak and the counters, quietly: Planner's DESIGN.md forbids
// gamification, so no big numbers -- one muted line each.
export function MetaLine({ state }) {
  const { streak, counts } = state;
  const last = state.last_checkin_at
    ? `последний ${dayWordInTz(state.last_checkin_at, state.timezone)} в ${formatHmInTz(state.last_checkin_at, state.timezone)}`
    : 'чек-инов ещё не было';
  return html`
    <p class="meta-line">
      Чек-ины <span class="mono">${streak}</span> ${pluralRu(streak, ['день', 'дня', 'дней'])} подряд · ${last}
      ${state.ignored_in_row > 0
        ? html` · проигнорировано подряд <span class="mono">${state.ignored_in_row}</span>`
        : null}
      <br />
      Воспоминаний <span class="mono">${counts.memories}</span> · забота
      <span class="mono">${counts.welfare_today}</span> · дистилляция
      <span class="mono">${counts.distill_today}</span> · поиск <span class="mono">${counts.search_today}</span>
    </p>
  `;
}
