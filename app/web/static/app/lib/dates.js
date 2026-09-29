// Calendar dates ("YYYY-MM-DD", the account's local date) as the
// backend sends them. Parsed by hand into UTC-midnight arithmetic,
// never through the Date string parser: `new Date('2026-09-23')` is UTC
// midnight, which displays as the previous day anywhere west of
// Greenwich. Moved out of screens/Checkin.js unchanged.

export const DAY_MS = 86400000;

const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTHS_GENITIVE = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
];
const WEEKDAYS = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
const WEEKDAYS_SHORT = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

// "YYYY-MM-DD" -> UTC-midnight milliseconds, or NaN if malformed.
export function parseLocalDate(s) {
  if (typeof s !== 'string' || s.length !== 10 || s[4] !== '-' || s[7] !== '-') return NaN;
  const y = Number(s.slice(0, 4));
  const m = Number(s.slice(5, 7));
  const d = Number(s.slice(8, 10));
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return NaN;
  if (m < 1 || m > 12 || d < 1 || d > 31) return NaN;
  return Date.UTC(y, m - 1, d);
}

export function dateKey(ms) {
  const dt = new Date(ms);
  const y = dt.getUTCFullYear();
  const m = String(dt.getUTCMonth() + 1).padStart(2, '0');
  const d = String(dt.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function shortDate(ms) {
  const dt = new Date(ms);
  return `${dt.getUTCDate()} ${MONTHS_SHORT[dt.getUTCMonth()]}`;
}

export function shortDateWithWeekday(ms) {
  const dt = new Date(ms);
  return `${WEEKDAYS_SHORT[dt.getUTCDay()]}, ${dt.getUTCDate()} ${MONTHS_SHORT[dt.getUTCMonth()]}`;
}

// «Сегодня» / «Вчера» / «23 сентября, среда», relative to `todayKey`.
export function dayHeading(key, todayKey) {
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
