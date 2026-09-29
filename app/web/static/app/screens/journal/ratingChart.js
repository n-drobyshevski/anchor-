// Дневник's «30 дней» card, moved out of the old Check-in screen: a
// single-series inline-SVG bar chart of daily ratings plus the history
// list, which is the chart's table view (every value the tooltip shows
// is also readable there). GET /api/checkins?days=30.
import { html } from '../../html.js';
import { useLayoutEffect, useRef, useState } from '../../../vendor/hooks.module.js';
import { useElementWidth } from '../../hooks.js';
import { pluralRu } from '../../lib/format.js';
import { DAY_MS, dateKey, parseLocalDate, shortDate, shortDateWithWeekday } from '../../lib/dates.js';
import { DUE_LABELS, ORDER_LABELS, RATINGS } from '../today/checkin.js';

export const CHART_DAYS = 30;

// ---------- «30 дней» chart ----------

// Numeric layout constants (px). The SVG's width is the measured card
// width, so every coordinate below is a real pixel, and text never
// scales with the viewport the way a fixed viewBox would.
const CH = {
  height: 168,
  top: 10,
  bottom: 22, // x-axis label band, inside the SVG's own height
  left: 18, // y tick labels
  right: 4,
  maxBar: 16,
  minBar: 3,
  radius: 4,
};

// A column path: square at the baseline, rounded at the data end.
// Built only from numbers.
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

function RatingChart({ days, endKey }) {
  const [wrapRef, width] = useElementWidth(320);
  const [active, setActive] = useState(null); // index into `slots`, or null
  const [announce, setAnnounce] = useState('');
  const tipRef = useRef(null);
  // A tap both presses and focuses the SVG; the focus handler must keep
  // the tapped day rather than jumping to its keyboard starting point.
  const pointerIdxRef = useRef(null);

  const byDate = new Map(days.map((c) => [c.local_date, c]));
  const endMs = parseLocalDate(endKey);
  const slots = [];
  if (!Number.isNaN(endMs)) {
    for (let i = CHART_DAYS - 1; i >= 0; i -= 1) {
      const ms = endMs - i * DAY_MS;
      slots.push({ ms, item: byDate.get(dateKey(ms)) || null });
    }
  }

  const plotW = Math.max(60, width - CH.left - CH.right);
  const plotH = CH.height - CH.top - CH.bottom;
  const band = plotW / Math.max(1, slots.length);
  const barW = Math.max(CH.minBar, Math.min(CH.maxBar, band - 3));
  const baseY = CH.top + plotH;
  const yFor = (v) => baseY - (v / 5) * plotH;
  const centerX = (i) => CH.left + band * i + band / 2;

  const rated = slots.filter((s) => s.item && s.item.rating);
  const avg = rated.length ? rated.reduce((sum, s) => sum + s.item.rating, 0) / rated.length : 0;
  const summary = rated.length
    ? `Оценки дня за 30 дней: ${rated.length} ${pluralRu(rated.length, ['день', 'дня', 'дней'])} с оценкой, `
      + `средняя ${avg.toFixed(1).replace('.', ',')}. Подробно — в списке ниже.`
    : 'Оценки дня за 30 дней: пока нет ни одной.';

  // Sparse x labels: the last day, then every 7th back from it.
  const labelIdx = [];
  for (let i = slots.length - 1; i >= 0; i -= 7) labelIdx.push(i);

  function tipText(i) {
    const s = slots[i];
    if (!s) return { value: '', label: '' };
    const label = shortDateWithWeekday(s.ms);
    if (!s.item || !s.item.rating) return { value: 'нет оценки', label };
    return { value: `${s.item.rating} из 5`, label };
  }

  // Position the tooltip over the active bar via CSSOM (never a style
  // attribute string), clamped inside the chart box.
  useLayoutEffect(() => {
    const tip = tipRef.current;
    if (!tip || active === null) return;
    const tipW = tip.offsetWidth || 0;
    const x = centerX(active) - tipW / 2;
    const clamped = Math.max(0, Math.min(width - tipW, x));
    tip.style.left = `${Math.round(clamped)}px`;
  });

  function indexFromPointer(e) {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left - CH.left;
    const i = Math.floor(x / band);
    return Math.max(0, Math.min(slots.length - 1, i));
  }

  function onPointerMove(e) {
    if (!slots.length) return;
    setActive(indexFromPointer(e));
  }

  function onPointerDown(e) {
    if (!slots.length) return;
    const i = indexFromPointer(e);
    pointerIdxRef.current = i;
    setActive(i);
  }

  function onPointerLeave(e) {
    if (document.activeElement !== e.currentTarget) setActive(null);
  }

  function moveTo(i) {
    const next = Math.max(0, Math.min(slots.length - 1, i));
    setActive(next);
    const t = tipText(next);
    setAnnounce(`${t.label}: ${t.value}`);
  }

  function onFocus() {
    if (!slots.length) return;
    if (pointerIdxRef.current !== null) {
      setActive(pointerIdxRef.current);
      pointerIdxRef.current = null;
      return;
    }
    // Start on the most recent rated day, else the last slot.
    let start = slots.length - 1;
    for (let i = slots.length - 1; i >= 0; i -= 1) {
      if (slots[i].item && slots[i].item.rating) {
        start = i;
        break;
      }
    }
    moveTo(active === null ? start : active);
  }

  function onKeyDown(e) {
    if (!slots.length) return;
    const cur = active === null ? slots.length - 1 : active;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
      e.preventDefault();
      moveTo(cur - 1);
    } else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
      e.preventDefault();
      moveTo(cur + 1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      moveTo(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      moveTo(slots.length - 1);
    } else if (e.key === 'Escape') {
      setActive(null);
    }
  }

  const tip = active !== null ? tipText(active) : null;

  return html`
    <div class="chart" ref=${wrapRef}>
      <svg
        class="chart-svg"
        width=${width}
        height=${CH.height}
        viewBox=${`0 0 ${width} ${CH.height}`}
        role="img"
        aria-label=${summary}
        aria-describedby="checkin-chart-hint"
        tabindex="0"
        onPointerMove=${onPointerMove}
        onPointerDown=${onPointerDown}
        onPointerLeave=${onPointerLeave}
        onFocus=${onFocus}
        onBlur=${() => setActive(null)}
        onKeyDown=${onKeyDown}
      >
        ${RATINGS.map(
          (v) => html`
            <line
              key=${`g${v}`}
              class="chart-grid"
              x1=${CH.left}
              x2=${CH.left + plotW}
              y1=${yFor(v)}
              y2=${yFor(v)}
            />
            <text key=${`t${v}`} class="chart-tick" x=${CH.left - 6} y=${yFor(v) + 4} text-anchor="end">${v}</text>
          `,
        )}
        <line class="chart-axis" x1=${CH.left} x2=${CH.left + plotW} y1=${baseY} y2=${baseY} />
        ${slots.map((s, i) => {
          const x = centerX(i) - barW / 2;
          const isActive = active === i;
          if (s.item && s.item.rating) {
            const y = yFor(s.item.rating);
            return html`
              <path
                key=${`b${i}`}
                class=${`chart-bar${isActive ? ' is-active' : ''}`}
                d=${barPath(x, y, barW, baseY - y, CH.radius)}
              />
            `;
          }
          return html`
            <rect
              key=${`e${i}`}
              class=${`chart-empty${isActive ? ' is-active' : ''}`}
              x=${x}
              y=${baseY - 2}
              width=${barW}
              height=${2}
            />
          `;
        })}
        ${active !== null
          ? html`
              <line
                class="chart-cursor"
                x1=${centerX(active)}
                x2=${centerX(active)}
                y1=${CH.top}
                y2=${baseY}
              />
            `
          : null}
        ${labelIdx.map((i) => {
          const last = i === slots.length - 1;
          return html`
            <text
              key=${`x${i}`}
              class="chart-tick"
              x=${last ? CH.left + plotW : centerX(i)}
              y=${CH.height - 6}
              text-anchor=${last ? 'end' : 'middle'}
            >
              ${shortDate(slots[i].ms)}
            </text>
          `;
        })}
      </svg>
      <div class="chart-tip" ref=${tipRef} hidden=${!tip} aria-hidden="true">
        ${tip ? html`<strong class="chart-tip-value">${tip.value}</strong><span class="chart-tip-label">${tip.label}</span>` : null}
      </div>
      <p id="checkin-chart-hint" class="sr-only">Стрелки влево и вправо — перейти между днями.</p>
      <p class="sr-only" aria-live="polite">${announce}</p>
    </div>
  `;
}

function HistoryList({ items }) {
  const rows = items.slice().reverse();
  if (!rows.length) return html`<p class="field-hint">Пока нет чек-инов.</p>`;
  return html`
    <ul class="history-list checkin-history">
      ${rows.map((c) => {
        const ms = parseLocalDate(c.local_date);
        const details = [];
        if (c.due_result && c.due_result !== 'none') details.push(`действие: ${DUE_LABELS[c.due_result] || c.due_result}`);
        for (const o of c.orders || []) details.push(`${o.text}: ${ORDER_LABELS[o.result] || o.result}`);
        return html`
          <li class="history-row" key=${c.local_date}>
            <div class="history-main">
              <p class="field-value">${Number.isNaN(ms) ? c.local_date : shortDateWithWeekday(ms)}</p>
              ${details.length ? html`<p class="field-hint">${details.join(' · ')}</p>` : null}
              ${c.note ? html`<p class="field-hint checkin-note-text">${c.note}</p>` : null}
            </div>
            <span class="status-chip rating-chip" data-rating=${c.rating ? String(c.rating) : 'none'}>
              ${c.rating ? `${c.rating}/5` : '—'}
            </span>
          </li>
        `;
      })}
    </ul>
  `;
}

export function MonthSection({ range, failed, onRetry }) {
  return html`
    <section class="card" aria-labelledby="checkin-range-heading">
      <h2 id="checkin-range-heading">30 дней</h2>
      ${failed && !range
        ? html`
            <p class="field-hint">
              Не удалось загрузить.
              <button type="button" class="link-button" onClick=${onRetry}>Повторить</button>
            </p>
          `
        : !range
          ? html`<div aria-busy="true" class="chart-placeholder"></div>`
          : html`
              <p class="field-hint">Оценка дня, 1–5</p>
              <${RatingChart} days=${range.items} endKey=${range.to} />
              <h3 class="subheading">История</h3>
              <${HistoryList} items=${range.items} />
            `}
    </section>
  `;
}
