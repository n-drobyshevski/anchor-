// Настройки (#/settings): this browser's appearance, the persona's
// intensity, the account's time zone, the integrations (Obsidian
// notes, the planner, Claude), the background work with undo, and
// Claude's write limits.
import { html } from '../html.js';
import { LoadError } from '../ui/ScreenState.js';
import { useAppState } from './useAppState.js';
import { DigestCard, IntegrationsCard } from './settings/integrations.js';
import { AppearanceCard, ClaudeLimitsCard, IntensityCard, TimezoneCard } from './settings/settingsCards.js';

const limitsToast = (data) => (data.vault_pending ? 'Сохранено, vault обновится позже.' : 'Сохранено.');

export function Settings() {
  const { state, failed, reload, applyMutation } = useAppState();
  // Оформление is local to this browser, so it shows even while
  // /api/state is loading or has failed.
  if (!state) {
    return html`
      <div class="screen-wrap">
        <div class="screen screen-settings" aria-busy=${failed ? 'false' : 'true'}>
          <${AppearanceCard} />
          ${failed ? html`<${LoadError} onRetry=${reload} />` : null}
        </div>
      </div>
    `;
  }

  return html`
    <div class="screen-wrap">
      <div class="screen screen-settings">
        <${AppearanceCard} />
        <${IntensityCard}
          value=${state.intensity}
          min=${state.limits.intensity_min}
          max=${state.limits.intensity_max}
          onSet=${(value) => applyMutation('/api/state/intensity', { value })}
        />
        <${TimezoneCard} tz=${state.timezone} onChange=${(tz) => applyMutation('/api/state/timezone', { tz })} />
        <${IntegrationsCard} />
        <${DigestCard} />
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
