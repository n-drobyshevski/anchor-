// GET /api/state and its mutations, shared by Сегодня (the day's
// action, focus, quiet, pause) and Настройки (time zone, Claude's write
// limits). Returns {state, failed, reload, applyMutation, togglePause}.
//
// Every mutation but pause is optimistic-free: POST, then replace the
// state with exactly the StateDTO the response returned -- never a
// local guess. replace() also orphans any GET still in flight, so a
// read that started before the write cannot land after it.
import { useEffect } from '../../vendor/hooks.module.js';
import { paused, pushToast } from '../store.js';
import { useResource } from '../hooks.js';
import { send } from '../lib/request.js';

export function useAppState() {
  // 'checkin' (streak/last_checkin_at) and 'memory' (counts.memories),
  // not only 'state': app/web/tail.py maps those fields to their own
  // topics.
  const { data, failed, reload, replace } = useResource('/api/state', ['state', 'checkin', 'memory']);

  useEffect(() => {
    if (data) paused.value = !!data.paused;
  }, [data]);

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

  // Pause has no `state` in its response (202 {}): it goes through the
  // ingress as a synthetic /out or /in, and the switch catches up once
  // the tail's invalidate("state") refetches. Returns whether the
  // request was accepted; failures are toasted here, since the row has
  // no inline error line.
  async function togglePause(on) {
    const res = await send('/api/state/pause', { on });
    if (res.status === 202) {
      pushToast('Сохранено.');
      return true;
    }
    if (res.status !== 401) pushToast(res.error || 'Не сохранено.');
    return false;
  }

  return { state: data, failed, reload, applyMutation, togglePause };
}
