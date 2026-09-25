// The state screen (#/state, nav label "Состояние"): read/write the
// StateDTO the W2 HTTP contract defines (GET /api/state, POST
// /api/state/{due,focus,quiet,timezone,pause}). Three cards (Действие,
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
import { apiGet, apiPost } from '../api.js';
import { forceLogout, pushToast, screenSubtitle } from '../store.js';
import { useAutoRefetch } from '../hooks.js';
import { Toasts } from '../ui/Toasts.js';
import { pluralRu } from './Checkin.js';

// 422 `detail` -> inline Russian text, shared by every field that can
// return one (due, quiet, timezone all key into the same table; a
// detail this screen has never seen falls back to a generic message
// rather than showing nothing).
const DETAIL_MESSAGES = {
  empty: 'Пусто',
  too_long: 'Слишком длинно',
  past: 'Время уже прошло',
  bad_time: 'Неверное время',
  unknown_timezone: 'Неизвестный пояс',
};

const FALLBACK_TIMEZONES = [
  'UTC', 'Europe/Moscow', 'Europe/Kaliningrad', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
  'Europe/Kyiv', 'Asia/Yekaterinburg', 'Asia/Novosibirsk', 'Asia/Krasnoyarsk', 'Asia/Irkutsk',
  'Asia/Yakutsk', 'Asia/Vladivostok', 'Asia/Almaty', 'Asia/Tashkent', 'Asia/Tbilisi', 'Asia/Yerevan',
  'Asia/Baku', 'Asia/Dubai', 'Asia/Istanbul', 'Asia/Jerusalem', 'Asia/Kolkata', 'Asia/Bangkok',
  'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Seoul', 'Australia/Sydney', 'Pacific/Auckland',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Sao_Paulo',
];

function minutesText(retryAfterSeconds) {
  const m = Math.max(1, Math.ceil((retryAfterSeconds || 60) / 60));
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

// Browser-local fallbacks, used only if the `tz`-aware formatters below
// throw (an unrecognized zone string reaching the browser's Intl, e.g.
// a zone this build's data does not know) -- otherwise every on-screen
// instant is formatted in `state.timezone`, never the viewer's own
// browser zone, which can easily be a different one (roadmap review
// finding: a `quiet_until`/`next_planned_for`/`focus.since` the backend
// computed against the account's own timezone must not be *displayed*
// in whatever zone the browser happens to sit in).
function formatHm(iso) {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString('ru-RU');
}

function formatHmInTz(iso, tz) {
  try {
    return new Date(iso).toLocaleTimeString('ru-RU', {
      timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
  } catch {
    return formatHm(iso);
  }
}

function formatDateInTz(iso, tz) {
  try {
    return new Date(iso).toLocaleDateString('ru-RU', { timeZone: tz });
  } catch {
    return formatDate(iso);
  }
}

// The quiet card's own display: date *and* time (a clamped `until` can
// be days away, not just later today), in `tz`.
function formatQuietUntil(iso, tz) {
  try {
    return new Date(iso).toLocaleString('ru-RU', {
      timeZone: tz, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
  } catch {
    return formatHm(iso);
  }
}

function usd(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

// Formats an absolute instant as ISO-8601 with an explicit numeric
// offset (never the `Z` shorthand the contract's "ISO-8601 with
// offset" phrasing does not ask for). `offsetMin` is minutes *east* of
// UTC -- callers below hand in an already-corrected value, since both
// `Date#getTimezoneOffset()` and this module's own `tzOffsetMinutes()`
// need a sign flip or a parse step first.
function isoWithOffset(instantMs, offsetMin) {
  const sign = offsetMin < 0 ? '-' : '+';
  const abs = Math.abs(offsetMin);
  const oh = String(Math.floor(abs / 60)).padStart(2, '0');
  const om = String(abs % 60).padStart(2, '0');
  const local = new Date(instantMs + offsetMin * 60000);
  const y = local.getUTCFullYear();
  const mo = String(local.getUTCMonth() + 1).padStart(2, '0');
  const d = String(local.getUTCDate()).padStart(2, '0');
  const h = String(local.getUTCHours()).padStart(2, '0');
  const mi = String(local.getUTCMinutes()).padStart(2, '0');
  const s = String(local.getUTCSeconds()).padStart(2, '0');
  return `${y}-${mo}-${d}T${h}:${mi}:${s}${sign}${oh}:${om}`;
}

// "N hours from now", as an absolute instant -- correct regardless of
// timezone, so this uses the browser's own local offset rather than
// state.timezone (an instant needs *an* offset to spell as ISO, not
// any particular one).
function isoPlusHours(hours) {
  const instantMs = Date.now() + hours * 3600000;
  return isoWithOffset(instantMs, -new Date(instantMs).getTimezoneOffset());
}

// Resolves `tz`'s UTC offset (minutes, east-positive) at a given
// instant via Intl rather than a bundled tz table, so this stays
// correct across a DST transition. Deliberately *not*
// `timeZoneName: 'longOffset'` (simpler, but only in Safari 15.4+ --
// the same cutoff as `Intl.supportedValuesOf`, below the fallback
// timezone list this screen already carries for exactly that reason;
// unlike that list, `tzOffsetMinutes` has no fallback of its own, so
// on an older Safari it used to throw synchronously inside the "До
// утра" button's onClick, before `apply()` ever ran -- no request, no
// busy state, no error, the button just silently did nothing).
// Formatting the wall-clock date/time parts in `tz` and diffing that
// against `atMs` works on every engine `Intl.DateTimeFormat` itself
// runs on, with no format string to parse.
function tzOffsetMinutes(tz, atMs) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(atMs));
  const get = (type) => Number(parts.find((p) => p.type === type).value);
  const asUtcMs = Date.UTC(
    get('year'), get('month') - 1, get('day'), get('hour') % 24, get('minute'), get('second'),
  );
  return Math.round((asUtcMs - Math.floor(atMs / 1000) * 1000) / 60000);
}

// Next 08:00 wall-clock time in `tz`, as an ISO instant carrying that
// zone's own offset. Two passes: the first guesses the target UTC
// instant using `tz`'s offset *now*; the second re-resolves the offset
// at that guess and re-solves for it, which is exact unless the zone
// changes offset (a DST transition) in the few hours the two guesses
// can differ by around midnight -- not worth a bundled tz library for
// one "tomorrow morning" preset button.
function isoNextMorning(tz) {
  const now = Date.now();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date(now));
  const get = (type) => Number(parts.find((p) => p.type === type).value);
  let y = get('year');
  let mo = get('month');
  let d = get('day');
  const hh = get('hour') % 24; // some engines render midnight as "24" under hour12:false
  if (hh >= 8) {
    const next = new Date(Date.UTC(y, mo - 1, d + 1));
    y = next.getUTCFullYear();
    mo = next.getUTCMonth() + 1;
    d = next.getUTCDate();
  }
  let offset = tzOffsetMinutes(tz, now);
  let guessMs = Date.UTC(y, mo - 1, d, 8, 0, 0) - offset * 60000;
  const offset2 = tzOffsetMinutes(tz, guessMs);
  if (offset2 !== offset) {
    guessMs = Date.UTC(y, mo - 1, d, 8, 0, 0) - offset2 * 60000;
    offset = offset2;
  }
  return isoWithOffset(guessMs, offset);
}

function timezoneList() {
  if (typeof Intl.supportedValuesOf === 'function') {
    try {
      const zones = Intl.supportedValuesOf('timeZone');
      if (zones && zones.length) return zones;
    } catch {
      // fall through to the static fallback below
    }
  }
  return FALLBACK_TIMEZONES;
}

// `timezoneList()` alone is not a complete option list: verified via
// node, `Intl.supportedValuesOf('timeZone')` returns CLDR's legacy
// canonical names (418 entries, including "Europe/Kiev" and
// "Asia/Calcutta") and always omits "UTC" outright -- so a zone
// app/core/commands.py's `set_timezone` accepts via `ZoneInfo`
// (`/tz Europe/Kyiv`, `/tz UTC`, or a fallback-list zone like
// "Asia/Kolkata" some users are stored under) can be missing from the
// browser's own list, leaving `tz` matching no `<option>` and the
// select showing blank or the wrong zone. Ensuring the *current* `tz`
// and "UTC" are always present, ahead of the rest, fixes that without
// needing this build to carry a second, exhaustive alias table.
function timezoneOptions(tz) {
  const zones = timezoneList();
  const extra = [];
  if (!zones.includes('UTC')) extra.push('UTC');
  if (tz && !zones.includes(tz) && tz !== 'UTC') extra.push(tz);
  return extra.length ? [...extra, ...zones] : zones;
}

// «сегодня» / «вчера» / a date, for the instant `iso` as seen in `tz`.
function dayWordInTz(iso, tz) {
  try {
    const key = (ms) => new Date(ms).toLocaleDateString('en-CA', { timeZone: tz });
    const that = key(Date.parse(iso));
    if (that === key(Date.now())) return 'сегодня';
    if (that === key(Date.now() - 86400000)) return 'вчера';
  } catch {
    // fall through to the plain date
  }
  return formatDateInTz(iso, tz);
}

// «HH:MM» when `iso` is today in `tz`, otherwise «вчера, HH:MM» / a
// date and time: a bare time for an instant days ago would read as
// today's.
function sinceText(iso, tz) {
  const day = dayWordInTz(iso, tz);
  const hm = formatHmInTz(iso, tz);
  return day === 'сегодня' ? hm : `${day}, ${hm}`;
}

// ---------- Действие на сегодня ----------

function DueCard({ due, dueMaxLen, timezone, onSave }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(due.action || '');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const textareaRef = useRef(null);
  const editButtonRef = useRef(null);
  const wasEditingRef = useRef(false);

  useEffect(() => {
    if (editing && textareaRef.current) textareaRef.current.focus();
    // Editing just closed (Escape, Сохранить or a successful save) --
    // return focus to the control that opened it, the same place a
    // native disclosure widget would leave it, instead of letting it
    // fall to <body> when the textarea/buttons unmount. Guarded by
    // `wasEditingRef` so this does not steal focus on the card's very
    // first render, when `editing` is already false and nothing closed.
    if (!editing && wasEditingRef.current && editButtonRef.current) editButtonRef.current.focus();
    wasEditingRef.current = editing;
  }, [editing]);

  function startEdit() {
    setDraft(due.action || '');
    setError('');
    setEditing(true);
  }

  function cancelEdit() {
    setDraft(due.action || '');
    setError('');
    setEditing(false);
  }

  async function save() {
    setBusy(true);
    setError('');
    const result = await onSave(draft);
    setBusy(false);
    if (result === true) setEditing(false);
    else setError(result);
  }

  function onKeyDown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancelEdit();
      return;
    }
    // Plain Enter inserts a newline (this is a multi-line textarea, not
    // a single-line field submitted by Enter) -- Ctrl/Cmd+Enter is the
    // keyboard shortcut to save without reaching for the mouse.
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!busy) save();
    }
  }

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
                ref=${editButtonRef}
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
        <span class="field-hint">${applying ? 'Применяется…' : 'Anchor ответит в чате'}</span>
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
  const [state, setState] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);

  async function reload() {
    const res = await apiGet('/api/state');
    if (res.status === 401) {
      forceLogout();
      return;
    }
    if (res.ok && res.data) {
      setState(res.data);
      setLoadFailed(false);
    } else {
      setLoadFailed(true);
    }
  }

  // 'checkin' (streak/last_checkin_at) and 'memory' (counts.memories),
  // not only 'state' -- see hooks.js's useAutoRefetch comment for why
  // this screen needs more than its own topic.
  useAutoRefetch(['state', 'checkin', 'memory'], reload);

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

  // Shared by every field mutation except pause (see the module
  // comment above for why pause is different): POST, and on 200
  // replace `state` wholesale with the response's own StateDTO --
  // never a locally-guessed merge -- per the task brief's
  // "optimistic-free ... then replace screen data with the returned
  // state" rule.
  async function applyMutation(path, body) {
    const res = await apiPost(path, body);
    if (res.status === 401) {
      forceLogout();
      return 'Сессия истекла.';
    }
    if (res.status === 200 && res.data && res.data.state) {
      setState(res.data.state);
      pushToast('Сохранено.');
      return true;
    }
    if (res.status === 422) {
      const detail = res.data && res.data.detail;
      return DETAIL_MESSAGES[detail] || 'Неверное значение.';
    }
    if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
      return 'Слишком много попыток.';
    }
    return 'Не сохранено.';
  }

  const saveDue = (text) => applyMutation('/api/state/due', { text });
  const toggleFocus = (on) => applyMutation('/api/state/focus', { on });
  const setQuiet = (until) => applyMutation('/api/state/quiet', { until });
  const changeTz = (tz) => applyMutation('/api/state/timezone', { tz });

  // Pause has no `state` in its response (202 {}) -- the toggle's own
  // paint (PauseCard's aria-checked) only catches up once the tail's
  // invalidate("state") round-trips into a fresh `reload()` above, the
  // same path Telegram-made changes already take. Returns a plain
  // boolean, not the string-or-true shape `applyMutation` returns:
  // PauseCard needs only "did this request get accepted" (to start its
  // own `pendingTarget` wait), and every failure branch here already
  // pushes its own toast, so there is no inline error text to carry
  // back the way the other cards' error strings do.
  async function togglePause(on) {
    const res = await apiPost('/api/state/pause', { on });
    if (res.status === 401) {
      forceLogout();
      return false;
    }
    if (res.status === 202) {
      pushToast('Сохранено.');
      return true;
    }
    if (res.status === 429) {
      pushToast(minutesText(res.data && res.data.retry_after));
      return false;
    }
    pushToast('Не сохранено.');
    return false;
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
  if (!state) {
    return html`<div class="screen-wrap"><div class="screen" aria-busy="true"></div></div>`;
  }

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
        <${MetaLine} state=${state} />
      </div>
      <${Toasts} />
    </div>
  `;
}
