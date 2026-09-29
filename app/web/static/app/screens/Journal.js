// Дневник (#/journal): looking back -- the 30-day rating chart with the
// check-in history, the journal feed, and the proposals already
// decided. Moved out of the old Check-in and Proposals screens.
import { html } from '../html.js';
import { usePagedResource, useResource } from '../hooks.js';
import { LoadError } from '../ui/ScreenState.js';
import { JournalSection } from './journal/journalFeed.js';
import { CHART_DAYS, MonthSection } from './journal/ratingChart.js';
import { HistoryRow, useProposals } from './today/proposals.js';

const JOURNAL_PAGE = 30;
// GET /api/journal's own cap (contract: limit max 50).
const JOURNAL_MAX_LIMIT = 50;

function journalQuery(offset, limit) {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  return `/api/journal?${params.toString()}`;
}

const hasItems = (d) => Array.isArray(d.items);

function ProposalsHistory({ proposals }) {
  const { data, failed, reload } = proposals;
  let body;
  if (!data) body = failed ? html`<${LoadError} onRetry=${reload} />` : html`<div aria-busy="true"></div>`;
  else if (!data.recent.length) body = html`<p class="field-hint">Пока пусто</p>`;
  else {
    body = html`
      <ul class="card-list">
        ${data.recent.map((item) => html`<${HistoryRow} key=${item.id} item=${item} />`)}
      </ul>
    `;
  }
  return html`
    <section class="card" aria-labelledby="proposals-history-heading">
      <h2 id="proposals-history-heading">Предложения</h2>
      ${body}
    </section>
  `;
}

export function Journal() {
  // 'checkin' covers check-in rows and journal rows (the tail maps the
  // journal field to it).
  const range = useResource(`/api/checkins?days=${CHART_DAYS}`, 'checkin', { accept: hasItems });
  const journal = usePagedResource(journalQuery, 'checkin', { pageSize: JOURNAL_PAGE, maxLimit: JOURNAL_MAX_LIMIT });
  const proposals = useProposals();

  return html`
    <div class="screen-wrap screen-wrap-wide">
      <div class="screen screen-journal screen-columns">
        <div class="screen-column">
          <${MonthSection} range=${range.data} failed=${range.failed} onRetry=${range.reload} />
          <${ProposalsHistory} proposals=${proposals} />
        </div>
        <div class="screen-column">
          <${JournalSection}
            items=${journal.items}
            total=${journal.total}
            loaded=${journal.loaded}
            failed=${journal.failed}
            loadingMore=${journal.loadingMore}
            todayKey=${range.data ? range.data.to : ''}
            onMore=${journal.loadMore}
            onRetry=${journal.reload}
          />
        </div>
      </div>
    </div>
  `;
}
