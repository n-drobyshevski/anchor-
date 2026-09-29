// Настройки's cards, moved out of the old State screen: the account's
// time zone and Claude's write limits. screens/Settings.js owns the
// StateDTO and the mutations.
import { html } from '../../html.js';
import { useEffect, useMemo, useState } from '../../../vendor/hooks.module.js';
import { dayWordInTz, formatHmInTz, timezoneOptions } from '../../lib/tz.js';

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

// Часовой пояс as a card of its own (it used to be a row of State's
// Режим card).
export function TimezoneCard({ tz, onChange }) {
  return html`
    <section class="card" aria-labelledby="tz-card-heading">
      <h2 id="tz-card-heading">Время</h2>
      <ul class="card-list">
        <${TimezoneRow} tz=${tz} onChange=${onChange} />
      </ul>
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
export function ClaudeLimitsCard({ limits, resetAt, timezone, onSet, onResetCounters }) {
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
