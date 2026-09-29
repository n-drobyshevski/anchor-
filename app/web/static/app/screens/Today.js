// Сегодня (#/today): everything about today on one screen -- a pending
// proposal when there is one, the day's action, today's check-in,
// Режим (focus, quiet, pause), today's spend and the metadata line.
// Replaces the old State, Proposals and Check-in screens' "today"
// parts; their history moved to Дневник and the settings to Настройки.
import { html } from '../html.js';
import { useEffect } from '../../vendor/hooks.module.js';
import { screenSubtitle } from '../store.js';
import { formatHmInTz } from '../lib/tz.js';
import { ScreenError, ScreenLoading } from '../ui/ScreenState.js';
import { useAppState } from './useAppState.js';
import { TodaySection, useCheckinToday } from './today/checkin.js';
import { PendingCard, useProposals } from './today/proposals.js';
import { DueCard, MetaLine, ModeCard, SpendCard } from './today/stateCards.js';

export function Today() {
  const app = useAppState();
  const checkin = useCheckinToday();
  const proposals = useProposals();
  const { state } = app;

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

  if (!state || !checkin.data) {
    const retry = () => {
      if (!state) app.reload();
      if (!checkin.data) checkin.reload();
    };
    return app.failed || checkin.failed
      ? html`<${ScreenError} wide onRetry=${retry} />`
      : html`<${ScreenLoading} wide />`;
  }

  const pending = proposals.data && proposals.data.pending;

  return html`
    <div class="screen-wrap screen-wrap-wide">
      <div class="screen screen-today screen-columns">
        <div class="screen-column">
          ${pending
            ? html`<${PendingCard} key=${pending.id} proposal=${pending} onDecide=${proposals.decide} />`
            : null}
          <${DueCard}
            due=${state.due}
            dueMaxLen=${state.limits.due_max_len}
            timezone=${state.timezone}
            onSave=${(text) => app.applyMutation('/api/state/due', { text })}
          />
          <${TodaySection} data=${checkin.data} onSubmit=${checkin.submit} />
        </div>
        <div class="screen-column">
          <${ModeCard}
            state=${state}
            onFocus=${(on) => app.applyMutation('/api/state/focus', { on })}
            onQuiet=${(until) => app.applyMutation('/api/state/quiet', { until })}
            onPause=${app.togglePause}
          />
          <${SpendCard} spend=${state.spend} />
          <${MetaLine} state=${state} />
        </div>
      </div>
    </div>
  `;
}
