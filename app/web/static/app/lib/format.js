// Text formatting shared by every screen: Russian plurals, the
// rate-limit message, money, and instants shown in the browser's own
// zone. Instants that belong to the account's timezone (state.timezone)
// go through lib/tz.js instead.

// pluralRu(5, ['день', 'дня', 'дней']) -> 'дней'.
export function pluralRu(n, forms) {
  const abs = Math.abs(n) % 100;
  const last = abs % 10;
  if (abs > 10 && abs < 20) return forms[2];
  if (last === 1) return forms[0];
  if (last >= 2 && last <= 4) return forms[1];
  return forms[2];
}

// The one wording for a 429, from the response's `retry_after`
// (seconds): seconds under a minute, whole minutes above.
export function retryText(retryAfterSeconds) {
  const s = Number(retryAfterSeconds) || 60;
  if (s < 60) {
    const n = Math.max(1, Math.ceil(s));
    return `Слишком много попыток — попробуй через ${n} ${pluralRu(n, ['секунду', 'секунды', 'секунд'])}.`;
  }
  return `Слишком много попыток — попробуй через ${Math.ceil(s / 60)} мин.`;
}

export function usd(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

// «HH:MM» in the browser's zone; '' for an unparseable value.
export function clockTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// Date and time in the browser's zone, for tooltips and history rows.
export function formatDateTime(iso) {
  return new Date(iso).toLocaleString('ru-RU');
}
