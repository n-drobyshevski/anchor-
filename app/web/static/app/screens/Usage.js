// Лимиты (#/usage): how much has been used against every cap -- today's
// spend, the OpenRouter key, 14 days of spend, the daily quotas and
// Claude's write caps. GET /api/usage and GET /api/usage/openrouter
// (a separate request: it asks OpenRouter, so it may be slower, and a
// failure there must not hide the rest). Read-only.
import { html } from '../html.js';
import { useResource } from '../hooks.js';
import { ScreenError, ScreenLoading } from '../ui/ScreenState.js';
import { ClaudeUsageCard, HistoryCard, OpenRouterCard, QuotasCard, TodayCard } from './usage/usageCards.js';

export function Usage() {
  const { data, failed, reload } = useResource('/api/usage', ['state', 'settings']);
  const openrouter = useResource('/api/usage/openrouter', []);
  if (!data) return failed ? html`<${ScreenError} onRetry=${reload} />` : html`<${ScreenLoading} />`;

  return html`
    <div class="screen-wrap">
      <div class="screen screen-usage">
        <${TodayCard} spend=${data.spend} />
        <${OpenRouterCard} data=${openrouter.data} failed=${openrouter.failed} onRetry=${openrouter.reload} />
        <${HistoryCard} spend=${data.spend} />
        ${data.quotas.length ? html`<${QuotasCard} quotas=${data.quotas} />` : null}
        ${data.claude ? html`<${ClaudeUsageCard} rows=${data.claude} />` : null}
      </div>
    </div>
  `;
}
