// Дневник's «Журнал» card, moved out of the old Check-in screen:
// journal lines grouped by local day, paged with «Показать ещё».
// GET /api/journal.
import { html } from '../../html.js';
import { clockTime, pluralRu } from '../../lib/format.js';
import { dayHeading } from '../../lib/dates.js';

// ---------- «Журнал» ----------

function groupByDay(items) {
  const groups = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.key === item.local_date) last.items.push(item);
    else groups.push({ key: item.local_date, items: [item] });
  }
  return groups;
}

export function JournalSection({ items, total, loaded, failed, loadingMore, todayKey, onMore, onRetry }) {
  const groups = groupByDay(items);
  const hasMore = items.length < total;
  return html`
    <section class="card" aria-labelledby="checkin-journal-heading">
      <div class="card-row">
        <h2 id="checkin-journal-heading" class="heading-with-badge">
          Журнал
          ${loaded
            ? html`
                <span class="nav-badge" aria-hidden="true">${total}</span>
                <span class="sr-only">, ${total} ${pluralRu(total, ['запись', 'записи', 'записей'])}</span>
              `
            : null}
        </h2>
      </div>
      ${failed && !items.length
        ? html`
            <p class="field-hint" role="alert">
              Не удалось загрузить.
              <button type="button" class="link-button" onClick=${onRetry}>Повторить</button>
            </p>
          `
        : !loaded
          ? html`<div aria-busy="true"></div>`
          : groups.length
            ? html`
                <div class="journal">
                  ${groups.map(
                    (g) => html`
                      <div class="journal-day" key=${g.key}>
                        <h3 class="subheading">${dayHeading(g.key, todayKey)}</h3>
                        <ul class="journal-list">
                          ${g.items.map(
                            (it) => html`
                              <li key=${it.id} class="journal-item">
                                ${it.created_at
                                  ? html`<span class="journal-time mono">${clockTime(it.created_at)}</span>`
                                  : null}
                                <span>${it.text}</span>
                              </li>
                            `,
                          )}
                        </ul>
                      </div>
                    `,
                  )}
                </div>
              `
            : html`<p class="field-hint">Журнал пока пуст — Echo добавляет сюда заметки из разговоров.</p>`}
      ${hasMore
        ? html`
            <div class="card-footer">
              <span class="field-hint">
                <span class="mono">${items.length}</span> из <span class="mono">${total}</span>
              </span>
              <button type="button" class="btn" disabled=${loadingMore} onClick=${onMore}>Показать ещё</button>
            </div>
          `
        : null}
    </section>
  `;
}
