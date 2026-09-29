// Лимиты's cards: today's spend against its caps, the OpenRouter key,
// 14 days of spend as a diagram-style bar chart (its day list is the
// table view), the daily quotas, and Claude's write caps. Read-only:
// the caps themselves are changed in Настройки or Telegram.
import { html } from '../../html.js';
import { useElementWidth } from '../../hooks.js';
import { usd } from '../../lib/format.js';
import { parseLocalDate, shortDate, shortDateWithWeekday } from '../../lib/dates.js';
import { LoadError } from '../../ui/ScreenState.js';

// A used/limit pair as a thin bar with the numbers beside it. Reaching
// the cap adds the word «лимит», so it never rests on colour alone.
function Meter({ id, label, used, limit, format = String, hint = '' }) {
  const reached = limit !== null && limit !== undefined && used >= limit;
  return html`
    <li class="row row-stacked usage-meter">
      <div class="row-head">
        <span class="row-title" id=${id}>${label}</span>
        <span class="mono usage-numbers">
          ${format(used)} / ${format(limit)}${reached ? html` <span class="usage-reached">лимит</span>` : null}
        </span>
      </div>
      <progress
        aria-labelledby=${id}
        aria-valuetext=${`${format(used)} из ${format(limit)}${reached ? ', лимит' : ''}`}
        value=${Math.min(used, limit || 0)}
        max=${Math.max(limit || 0, 1e-9)}
      ></progress>
      ${hint ? html`<span class="field-hint">${hint}</span>` : null}
    </li>
  `;
}

const plain = (n) => String(n);
const kb = (n) => `${Math.floor(n / 1024)} КБ`;

// ---------- Сегодня ----------

export function TodayCard({ spend }) {
  return html`
    <section class="card" aria-labelledby="usage-today-heading">
      <h2 id="usage-today-heading">Сегодня</h2>
      <ul class="card-list">
        <${Meter} id="usage-spend" label="Траты" used=${spend.today_usd} limit=${spend.cap_usd} format=${usd} />
        ${spend.idle_cap_usd !== null
          ? html`<${Meter}
              id="usage-idle"
              label="Фоновая работа"
              used=${spend.idle_today_usd}
              limit=${spend.idle_cap_usd}
              format=${usd}
              hint="Входит в общие траты."
            />`
          : null}
      </ul>
    </section>
  `;
}

// ---------- OpenRouter ----------

function OpenRouterRow({ label, value }) {
  return html`<li><span>${label}</span><span class="mono">${value}</span></li>`;
}

export function OpenRouterCard({ data, failed, onRetry }) {
  let body;
  if (!data) {
    body = failed ? html`<${LoadError} onRetry=${onRetry} />` : html`<p class="field-hint" aria-busy="true">Загрузка…</p>`;
  } else if (!data.available) {
    body = html`<p class="field-hint">Недоступно: OpenRouter не ответил. Попробуй позже.</p>`;
  } else {
    const rows = [];
    if (data.balance !== null && data.balance !== undefined) rows.push(['Баланс', usd(data.balance)]);
    if (data.limit !== null && data.limit !== undefined) {
      rows.push(['Лимит ключа', usd(data.limit)]);
      if (data.limit_remaining !== null && data.limit_remaining !== undefined) {
        rows.push(['Осталось по ключу', usd(data.limit_remaining)]);
      }
    } else {
      rows.push(['Лимит ключа', 'нет']);
    }
    if (data.usage_daily !== null && data.usage_daily !== undefined) rows.push(['За сегодня (UTC)', usd(data.usage_daily)]);
    if (data.usage_weekly !== null && data.usage_weekly !== undefined) rows.push(['За неделю', usd(data.usage_weekly)]);
    if (data.usage_monthly !== null && data.usage_monthly !== undefined) rows.push(['За месяц', usd(data.usage_monthly)]);
    if (data.usage !== null && data.usage !== undefined) rows.push(['Всего по ключу', usd(data.usage)]);
    body = html`
      <ul class="category-list">
        ${rows.map(([label, value]) => html`<${OpenRouterRow} key=${label} label=${label} value=${value} />`)}
      </ul>
    `;
  }
  return html`
    <section class="card" aria-labelledby="usage-openrouter-heading">
      <h2 id="usage-openrouter-heading">OpenRouter</h2>
      <p class="field-hint">Счёт провайдера моделей. Обновляется раз в пять минут.</p>
      ${body}
    </section>
  `;
}

// ---------- За 14 дней ----------

const CH = { height: 148, top: 10, bottom: 22, left: 36, right: 4, maxBar: 18, radius: 3 };

function barPath(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  const f = (n) => Math.round(n * 100) / 100;
  return (
    `M${f(x)} ${f(y + h)}` +
    `V${f(y + rr)}` +
    `Q${f(x)} ${f(y)} ${f(x + rr)} ${f(y)}` +
    `H${f(x + w - rr)}` +
    `Q${f(x + w)} ${f(y)} ${f(x + w)} ${f(y + rr)}` +
    `V${f(y + h)}Z`
  );
}

function SpendChart({ history, cap }) {
  const [wrapRef, width] = useElementWidth(320);
  const plotW = Math.max(width - CH.left - CH.right, 40);
  const plotH = CH.height - CH.top - CH.bottom;
  const top = Math.max(cap || 0, ...history.map((d) => d.usd), 0.01) * 1.1;
  const yFor = (v) => CH.top + plotH - (v / top) * plotH;
  const baseY = CH.top + plotH;
  const slot = plotW / history.length;
  const barW = Math.max(3, Math.min(CH.maxBar, slot * 0.6));
  const centerX = (i) => CH.left + slot * i + slot / 2;
  const labelIdx = [0, Math.floor((history.length - 1) / 2), history.length - 1];
  const total = history.reduce((sum, d) => sum + d.usd, 0);
  const summary = `Траты за ${history.length} дней: всего ${usd(total)}, дневной лимит ${usd(cap)}.`;

  return html`
    <div class="chart" ref=${wrapRef}>
      <svg class="chart-svg" width=${width} height=${CH.height} viewBox=${`0 0 ${width} ${CH.height}`} role="img" aria-label=${summary}>
        <line class="chart-axis" x1=${CH.left} x2=${CH.left + plotW} y1=${baseY} y2=${baseY} />
        ${cap
          ? html`
              <line class="chart-cap" x1=${CH.left} x2=${CH.left + plotW} y1=${yFor(cap)} y2=${yFor(cap)} />
              <text class="chart-tick" x=${CH.left - 6} y=${yFor(cap) + 4} text-anchor="end">${usd(cap)}</text>
            `
          : null}
        <text class="chart-tick" x=${CH.left - 6} y=${baseY + 4} text-anchor="end">$0</text>
        ${history.map((d, i) => {
          const x = centerX(i) - barW / 2;
          const ms = parseLocalDate(d.date);
          const title = `${shortDate(ms)}: ${usd(d.usd)}`;
          if (d.usd <= 0) {
            return html`<rect key=${d.date} class="chart-empty" x=${x} y=${baseY - 2} width=${barW} height=${2}><title>${title}</title></rect>`;
          }
          const y = Math.min(yFor(d.usd), baseY - 2);
          return html`<path key=${d.date} class="chart-bar" d=${barPath(x, y, barW, baseY - y, CH.radius)}><title>${title}</title></path>`;
        })}
        ${labelIdx.map((i) => {
          const last = i === history.length - 1;
          return html`
            <text key=${`x${i}`} class="chart-tick" x=${last ? CH.left + plotW : centerX(i)} y=${CH.height - 6} text-anchor=${last ? 'end' : 'middle'}>
              ${shortDate(parseLocalDate(history[i].date))}
            </text>
          `;
        })}
      </svg>
    </div>
  `;
}

export function HistoryCard({ spend }) {
  const history = spend.history || [];
  const models = Object.entries(spend.by_model || {});
  const days = history.filter((d) => d.usd > 0).reverse();
  return html`
    <section class="card" aria-labelledby="usage-history-heading">
      <div class="card-row">
        <h2 id="usage-history-heading">За ${history.length} дней</h2>
        <span class="mono spend-total">${usd(history.reduce((sum, d) => sum + d.usd, 0))}</span>
      </div>
      ${history.length ? html`<${SpendChart} history=${history} cap=${spend.cap_usd} />` : null}
      ${models.length
        ? html`
            <h3 class="subheading">По моделям</h3>
            <ul class="category-list">
              ${models.map(([name, amount]) => html`<li key=${name}><span class="mono usage-model">${name}</span><span class="mono">${usd(amount)}</span></li>`)}
            </ul>
          `
        : html`<p class="field-hint">Трат не было.</p>`}
      ${days.length
        ? html`
            <details class="usage-days">
              <summary>По дням</summary>
              <ul class="category-list">
                ${days.map(
                  (d) => html`
                    <li key=${d.date}>
                      <span>
                        ${shortDateWithWeekday(parseLocalDate(d.date))}
                        <span class="field-hint usage-day-split">
                          ${Object.entries(d.by_category)
                            .map(([name, amount]) => `${name} ${usd(amount)}`)
                            .join(' · ')}
                        </span>
                      </span>
                      <span class="mono">${usd(d.usd)}</span>
                    </li>
                  `,
                )}
              </ul>
            </details>
          `
        : null}
    </section>
  `;
}

// ---------- Квоты ----------

const WINDOW_WORDS = { day: 'в день', '24h': 'за сутки', hour: 'в час' };

export function QuotasCard({ quotas }) {
  return html`
    <section class="card" aria-labelledby="usage-quotas-heading">
      <h2 id="usage-quotas-heading">Квоты</h2>
      <ul class="card-list">
        ${quotas.map(
          (q) => html`<${Meter}
            key=${q.key}
            id=${`quota-${q.key}`}
            label=${`${q.label} ${WINDOW_WORDS[q.window] || ''}`.trim()}
            used=${q.used}
            limit=${q.limit}
            format=${plain}
          />`,
        )}
      </ul>
    </section>
  `;
}

// ---------- Claude ----------

export function ClaudeUsageCard({ rows }) {
  return html`
    <section class="card" aria-labelledby="usage-claude-heading">
      <h2 id="usage-claude-heading">Запись Claude</h2>
      <ul class="card-list">
        ${rows.map(
          (row) => html`<${Meter}
            key=${row.key}
            id=${`claude-${row.key}`}
            label=${row.label}
            used=${row.used}
            limit=${row.limit}
            format=${row.unit === 'bytes' ? kb : plain}
          />`,
        )}
      </ul>
      <div class="card-footer">
        <span class="field-hint">Счётчики обнуляются в Настройках.</span>
        <a class="btn" href="#/settings">Изменить</a>
      </div>
    </section>
  `;
}
