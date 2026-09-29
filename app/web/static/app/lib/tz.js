// Instants shown and computed in the account's own timezone
// (state.timezone), moved out of screens/State.js unchanged. Every
// on-screen instant the backend computed against that zone
// (`quiet_until`, `next_planned_for`, `focus.since`) is formatted here,
// never in whatever zone the browser happens to sit in; each formatter
// falls back to the browser's zone only if Intl rejects `tz`.
import { clockTime } from './format.js';

const FALLBACK_TIMEZONES = [
  'UTC', 'Europe/Moscow', 'Europe/Kaliningrad', 'Europe/London', 'Europe/Berlin', 'Europe/Paris',
  'Europe/Kyiv', 'Asia/Yekaterinburg', 'Asia/Novosibirsk', 'Asia/Krasnoyarsk', 'Asia/Irkutsk',
  'Asia/Yakutsk', 'Asia/Vladivostok', 'Asia/Almaty', 'Asia/Tashkent', 'Asia/Tbilisi', 'Asia/Yerevan',
  'Asia/Baku', 'Asia/Dubai', 'Asia/Istanbul', 'Asia/Jerusalem', 'Asia/Kolkata', 'Asia/Bangkok',
  'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Seoul', 'Australia/Sydney', 'Pacific/Auckland',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Sao_Paulo',
];

export function formatHmInTz(iso, tz) {
  try {
    return new Date(iso).toLocaleTimeString('ru-RU', {
      timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
  } catch {
    return clockTime(iso);
  }
}

function formatDateInTz(iso, tz) {
  try {
    return new Date(iso).toLocaleDateString('ru-RU', { timeZone: tz });
  } catch {
    return new Date(iso).toLocaleDateString('ru-RU');
  }
}

// Date *and* time (a clamped quiet `until` can be days away), in `tz`.
export function formatQuietUntil(iso, tz) {
  try {
    return new Date(iso).toLocaleString('ru-RU', {
      timeZone: tz, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    });
  } catch {
    return clockTime(iso);
  }
}

// «сегодня» / «вчера» / a date, for the instant `iso` as seen in `tz`.
export function dayWordInTz(iso, tz) {
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
export function sinceText(iso, tz) {
  const day = dayWordInTz(iso, tz);
  const hm = formatHmInTz(iso, tz);
  return day === 'сегодня' ? hm : `${day}, ${hm}`;
}

// An absolute instant as ISO-8601 with an explicit numeric offset
// (never `Z`). `offsetMin` is minutes *east* of UTC.
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

// "N hours from now" as an instant; any offset spells it, so the
// browser's own is used.
export function isoPlusHours(hours) {
  const instantMs = Date.now() + hours * 3600000;
  return isoWithOffset(instantMs, -new Date(instantMs).getTimezoneOffset());
}

// `tz`'s UTC offset (minutes, east-positive) at `atMs`, via Intl parts
// rather than `timeZoneName: 'longOffset'` (Safari 15.4+ only), so it
// works on every engine Intl.DateTimeFormat runs on and across DST.
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
// zone's offset. Two passes, exact unless the zone changes offset in
// the few hours the guesses can differ by around midnight.
export function isoNextMorning(tz) {
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

// The browser's list omits "UTC" and uses CLDR's legacy names
// ("Europe/Kiev"), so the current `tz` and "UTC" are always put first
// -- otherwise a zone the backend accepts could match no <option>.
export function timezoneOptions(tz) {
  const zones = timezoneList();
  const extra = [];
  if (!zones.includes('UTC')) extra.push('UTC');
  if (tz && !zones.includes(tz) && tz !== 'UTC') extra.push(tz);
  return extra.length ? [...extra, ...zones] : zones;
}
