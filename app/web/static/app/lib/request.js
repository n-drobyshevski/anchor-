// The one place an authenticated screen talks to /api. Wraps api.js
// (which stays deliberately dumb about status codes, for the login
// screen's sake) with the handling every panel used to repeat:
//
//   - 401 means the session died: forceLogout(), always.
//   - 429 becomes retryText(retry_after).
//   - 422 becomes the Russian text for its `detail`, from one table.
//   - anything else that is not 2xx becomes `fallback`.
//
// Neither function toasts. The caller shows `error` where the action
// was taken (inline under a field, or as a toast for a one-tap action),
// so a failure is never reported twice.
import { apiGet, apiPost } from '../api.js';
import { forceLogout } from '../store.js';
import { retryText } from './format.js';

// Every 422 `detail` any panel endpoint returns.
const DETAIL_MESSAGES = {
  // state
  empty: 'Пусто',
  too_long: 'Слишком длинно',
  past: 'Время уже прошло',
  bad_time: 'Неверное время',
  unknown_timezone: 'Неизвестный пояс',
  // claude write limits
  out_of_range: 'Вне допустимого диапазона',
  unknown_key: 'Нет такого лимита',
  // memory
  bad_kind: 'Неверный вид',
  // check-in
  rating: 'Выбери оценку от 1 до 5.',
  note_too_long: 'Заметка слишком длинная.',
  note_command: 'Заметка не может начинаться с «/».',
  due_result: 'Отметь, как прошло действие на сегодня.',
  orders: 'Список поручений изменился — форма обновлена.',
};

export function detailText(detail) {
  return DETAIL_MESSAGES[detail] || 'Неверное значение.';
}

// GET. Resolves to {ok, status, data}; `ok` only for a 2xx with a body.
export async function load(path) {
  const res = await apiGet(path);
  if (res.status === 401) forceLogout();
  return { ok: res.ok && res.data != null, status: res.status, data: res.data };
}

// POST. Resolves to {ok, status, data, error}: `error` is '' on a 2xx,
// otherwise the text to show.
export async function send(path, body, { fallback = 'Не сохранено.' } = {}) {
  const res = await apiPost(path, body);
  let error = '';
  if (res.status === 401) {
    forceLogout();
    error = 'Сессия истекла.';
  } else if (res.status === 429) {
    error = retryText(res.data && res.data.retry_after);
  } else if (res.status === 422) {
    error = detailText(res.data && res.data.detail);
  } else if (!res.ok) {
    error = fallback;
  }
  return { ok: res.ok, status: res.status, data: res.data, error };
}
