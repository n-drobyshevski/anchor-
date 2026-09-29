// The state screen (#/state, nav label "Состояние"): read/write the
// StateDTO the W2 HTTP contract defines (GET /api/state, POST
// /api/state/{due,focus,quiet,timezone,pause,claude-limits,
// claude-counters/reset}). Three cards (Действие,
// Режим's rows, Траты) and a quiet metadata line; each field's card or
// row owns its own edit/busy/error state; the screen component
// itself only owns the fetched StateDTO and the plumbing every card's
// mutation goes through.
//
// Every mutation but pause is optimistic-free per the task brief:
// POST, wait for the response, then replace the screen's state with
// exactly the `state` the response returned -- never a local guess.
// `/api/state/pause` is the one exception (contract: 202 {}, no
// state): it goes through the ingress as a synthetic /out or /in and
// is applied by the turn pipeline asynchronously, so this screen's
// `paused` flag only catches up once the tail's invalidate("state")
// arrives (useAutoRefetch below) and a fresh GET lands, exactly the
// same path a Telegram-made change already takes.
import { html } from '../html.js';
import { useEffect, useMemo, useRef, useState } from '../../vendor/hooks.module.js';
import { paused, pushToast, screenSubtitle } from '../store.js';
import { useResource } from '../hooks.js';
import { send } from '../lib/request.js';
import { pluralRu, usd } from '../lib/format.js';
import {
  dayWordInTz,
  formatHmInTz,
  formatQuietUntil,
  isoNextMorning,
  isoPlusHours,
  sinceText,
  timezoneOptions,
} from '../lib/tz.js';
import { ScreenError, ScreenLoading } from '../ui/ScreenState.js';
import { useInlineEdit } from '../ui/inlineEdit.js';

// ---------- Действие на сегодня ----------

function DueCard({ due, dueMaxLen, timezone, onSave }) {
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

// ---------- Режим: Фокус / Тишина / Пауза / Часовой пояс ----------

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

function TimezoneRow({ tz, onChange }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // Recomputed whenever `tz` itself changes (not built once and cached
  // in a ref for the component's whole lifetime) -- a zone set from
  // Telegram between page loads, or one this screen's own successful
  // change just returned, must also be present in the list it is about
  // to be selected from.
  const zones = useMemo(() => timezoneOptions(tz), [tz]);

  async function handleChange(e) {
    const value = e.target.value;
    setBusy(true);
    setError('');
    const result = await onChange(value);
    setBusy(false);
    if (result !== true) setError(result);
  }

  return html`
    <li class="row row-stacked">
      <label for="tz-select" class="row-title">Часовой пояс</label>
      <select id="tz-select" disabled=${busy} value=${tz} onChange=${handleChange}>
        ${zones.map((z) => html`<option key=${z} value=${z}>${z}</option>`)}
      </select>
      ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
    </li>
  `;
}

function ModeCard({ state, onFocus, onQuiet, onPause, onTimezone }) {
  return html`
    <section class="card" aria-labelledby="mode-heading">
      <h2 id="mode-heading">Режим</h2>
      <ul class="card-list">
        <${FocusRow} focus=${state.focus} timezone=${state.timezone} onToggle=${onFocus} />
        <${QuietRow} quietUntil=${state.quiet_until} timezone=${state.timezone} onSet=${onQuiet} />
        <${PauseRow} paused=${state.paused} onToggle=${onPause} />
        <${TimezoneRow} tz=${state.timezone} onChange=${onTimezone} />
      </ul>
    </section>
  `;
}

// ---------- Траты сегодня ----------

function SpendCard({ spend }) {
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

// ---------- Лимиты записи Claude ----------
// Claude's write caps (POST /api/state/claude-limits; the same values
// `/claude limits` shows in Telegram). `bytes_per_day` is edited in КБ
// and sent in bytes; every other cap is a plain count.

function toShown(item, v) {
  return item.unit === 'bytes' ? Math.floor(v / 1024) : v;
}

function fromShown(item, v) {
  return item.unit === 'bytes' ? v * 1024 : v;
}

function LimitRow({ item, onSet }) {
  const [draft, setDraft] = useState(String(toShown(item, item.value)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inputId = `limit-${item.key}`;
  const unit = item.unit === 'bytes' ? ' КБ' : '';

  // A value set elsewhere (Telegram, another tab) replaces the draft.
  useEffect(() => {
    setDraft(String(toShown(item, item.value)));
    setError('');
  }, [item.value]);

  const parsed = /^\d+$/.test(draft.trim()) ? Number(draft.trim()) : null;
  const dirty = parsed !== toShown(item, item.value);

  async function send(value) {
    setBusy(true);
    setError('');
    const result = await onSet(item.key, value);
    setBusy(false);
    if (result !== true) setError(result);
  }

  function save() {
    const lo = toShown(item, item.min);
    const hi = toShown(item, item.max);
    if (parsed === null || parsed < lo || parsed > hi) {
      setError(`Допустимо от ${lo} до ${hi}${unit}`);
      return;
    }
    send(fromShown(item, parsed));
  }

  function onKeyDown(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (!busy && dirty) save();
    }
  }

  const overridden = item.value !== item.default;
  const range = `${toShown(item, item.min)}–${toShown(item, item.max)}${unit}`;

  // One compact row per cap: label and range on the left, the number on
  // the right. «Сохранить» appears only once the draft differs, and
  // «сбросить» only while the cap is overridden -- nine always-present
  // buttons made the card a wall of disabled controls.
  return html`
    <li class="row limit-row">
      <div class="row-main">
        <label for=${inputId} class="row-title">${item.label}</label>
        <span class="field-hint" id=${`${inputId}-hint`}>
          ${range}${overridden
            ? html` · по умолчанию ${toShown(item, item.default)}${unit} ·${' '}<button
                  type="button"
                  class="link-button"
                  disabled=${busy}
                  onClick=${() => send(null)}
                  aria-label=${`Вернуть «${item.label}» к ${toShown(item, item.default)}${unit}`}
                >
                  сбросить
                </button>`
            : null}
        </span>
        ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
      </div>
      <div class="limit-controls">
        ${dirty
          ? html`<button type="button" class="btn btn-primary" disabled=${busy} onClick=${save}>Сохранить</button>`
          : null}
        <input
          id=${inputId}
          class="limit-input mono"
          type="number"
          inputmode="numeric"
          min=${toShown(item, item.min)}
          max=${toShown(item, item.max)}
          step="1"
          disabled=${busy}
          value=${draft}
          onInput=${(e) => setDraft(e.target.value)}
          onKeyDown=${onKeyDown}
          aria-describedby=${`${inputId}-hint`}
        />
      </div>
    </li>
  `;
}

// «Обнулить счётчики» (POST /api/state/claude-counters/reset; Telegram's
// /claude limits counters): every hourly and daily count starts over,
// the caps themselves stay as they are.
function ClaudeLimitsCard({ limits, resetAt, timezone, onSet, onResetCounters }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function resetCounters() {
    setBusy(true);
    setError('');
    const result = await onResetCounters();
    setBusy(false);
    if (result !== true) setError(result);
  }

  return html`
    <section class="card" aria-labelledby="claude-limits-heading">
      <h2 id="claude-limits-heading">Лимиты записи Claude</h2>
      <p class="field-hint">Сколько Claude может менять в заметках-знаниях. То же, что /claude limits в Telegram.</p>
      <ul class="card-list">
        ${limits.map((item) => html`<${LimitRow} key=${item.key} item=${item} onSet=${onSet} />`)}
      </ul>
      <div class="card-footer">
        ${error
          ? html`<p class="inline-error" role="alert">${error}</p>`
          : resetAt
            ? html`<span class="field-hint">счётчики обнулены ${dayWordInTz(resetAt, timezone)} в <span class="mono">${formatHmInTz(resetAt, timezone)}</span></span>`
            : html`<span></span>`}
        <button type="button" class="btn" disabled=${busy} onClick=${resetCounters}>Обнулить счётчики</button>
      </div>
    </section>
  `;
}

// ---------- metadata line ----------
// The streak and the counters, quietly: Planner's DESIGN.md forbids
// gamification, so no big numbers -- one muted line each.
function MetaLine({ state }) {
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

export function State() {
  // 'checkin' (streak/last_checkin_at) and 'memory' (counts.memories),
  // not only 'state': app/web/tail.py maps those fields to their own
  // topics.
  const { data: state, failed, reload, replace } = useResource('/api/state', ['state', 'checkin', 'memory']);

  useEffect(() => {
    if (state) paused.value = !!state.paused;
  }, [state]);

  // «следующее сообщение ~HH:MM» sits under the toolbar's title
  // (ui/Toolbar.js), not in this screen's body; cleared on unmount so
  // it never leaks onto another screen.
  const subtitle =
    state && state.next_planned_for
      ? `следующее сообщение ~${formatHmInTz(state.next_planned_for, state.timezone)}`
      : '';
  useEffect(() => {
    screenSubtitle.value = subtitle;
  }, [subtitle]);
  useEffect(() => () => {
    screenSubtitle.value = '';
  }, []);

  // Every field mutation but pause: POST, and on 200 replace `state`
  // wholesale with the response's own StateDTO -- never a locally
  // guessed merge. replace() also orphans any GET still in flight, so
  // a read that started before this write cannot land after it.
  // Returns true, or the text to show inline.
  async function applyMutation(path, body, okToast = () => 'Сохранено.') {
    const res = await send(path, body);
    if (res.status === 200 && res.data && res.data.state) {
      replace(res.data.state);
      pushToast(okToast(res.data));
      return true;
    }
    return res.error || 'Не сохранено.';
  }

  const saveDue = (text) => applyMutation('/api/state/due', { text });
  const toggleFocus = (on) => applyMutation('/api/state/focus', { on });
  const setQuiet = (until) => applyMutation('/api/state/quiet', { until });
  const changeTz = (tz) => applyMutation('/api/state/timezone', { tz });
  const limitsToast = (data) => (data.vault_pending ? 'Сохранено, vault обновится позже.' : 'Сохранено.');
  const setClaudeLimit = (key, value) => applyMutation('/api/state/claude-limits', { key, value }, limitsToast);
  const resetClaudeCounters = () => applyMutation('/api/state/claude-counters/reset', {}, limitsToast);

  // Pause has no `state` in its response (202 {}): PauseRow's switch
  // catches up once the tail's invalidate("state") refetches. Returns
  // whether the request was accepted; failures are toasted here, since
  // the row has no inline error line.
  async function togglePause(on) {
    const res = await send('/api/state/pause', { on });
    if (res.status === 202) {
      pushToast('Сохранено.');
      return true;
    }
    if (res.status !== 401) pushToast(res.error || 'Не сохранено.');
    return false;
  }

  if (!state) return failed ? html`<${ScreenError} onRetry=${reload} />` : html`<${ScreenLoading} />`;

  return html`
    <div class="screen-wrap">
      <div class="screen screen-state">
        <${DueCard}
          due=${state.due}
          dueMaxLen=${state.limits.due_max_len}
          timezone=${state.timezone}
          onSave=${saveDue}
        />
        <${ModeCard}
          state=${state}
          onFocus=${toggleFocus}
          onQuiet=${setQuiet}
          onPause=${togglePause}
          onTimezone=${changeTz}
        />
        <${SpendCard} spend=${state.spend} />
        ${state.claude_write_limits
          ? html`<${ClaudeLimitsCard}
              limits=${state.claude_write_limits}
              resetAt=${state.claude_counters_reset_at}
              timezone=${state.timezone}
              onSet=${setClaudeLimit}
              onResetCounters=${resetClaudeCounters}
            />`
          : null}
        <${MetaLine} state=${state} />
      </div>
    </div>
  `;
}
