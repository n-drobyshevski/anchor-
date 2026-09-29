// Настройки (#/settings): the persona's intensity, the account's time
// zone and Claude's write limits.
import { html } from '../html.js';
import { ScreenError, ScreenLoading } from '../ui/ScreenState.js';
import { useAppState } from './useAppState.js';
import { ClaudeLimitsCard, IntensityCard, TimezoneCard } from './settings/settingsCards.js';

const limitsToast = (data) => (data.vault_pending ? 'Сохранено, vault обновится позже.' : 'Сохранено.');

export function Settings() {
  const { state, failed, reload, applyMutation } = useAppState();
  if (!state) return failed ? html`<${ScreenError} onRetry=${reload} />` : html`<${ScreenLoading} />`;

  return html`
    <div class="screen-wrap">
      <div class="screen screen-settings">
        <${IntensityCard}
          value=${state.intensity}
          min=${state.limits.intensity_min}
          max=${state.limits.intensity_max}
          onSet=${(value) => applyMutation('/api/state/intensity', { value })}
        />
        <${TimezoneCard} tz=${state.timezone} onChange=${(tz) => applyMutation('/api/state/timezone', { tz })} />
        ${state.claude_write_limits
          ? html`<${ClaudeLimitsCard}
              limits=${state.claude_write_limits}
              resetAt=${state.claude_counters_reset_at}
              timezone=${state.timezone}
              onSet=${(key, value) => applyMutation('/api/state/claude-limits', { key, value }, limitsToast)}
              onResetCounters=${() => applyMutation('/api/state/claude-counters/reset', {}, limitsToast)}
            />`
          : null}
      </div>
    </div>
  `;
}
