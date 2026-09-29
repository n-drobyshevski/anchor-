// Настройки's «Подключения» and «Фоновая работа» cards (GET
// /api/settings, GET /api/digest).
//
// Подключения: Obsidian notes (the /vault notes switch), the planner's
// sync (/planner on|off), and Claude's connection -- read-only here:
// its library switches are Telegram-only, like /claude itself, so a
// stolen web session cannot open the notes to Claude. Each section
// shows only when its feature is deployed.
//
// Фоновая работа: /digest for the last 24 hours or 7 days, and an
// «Отменить» for every run that can still be undone.
import { html } from '../../html.js';
import { useState } from '../../../vendor/hooks.module.js';
import { pushToast } from '../../store.js';
import { useMountedRef, useResource } from '../../hooks.js';
import { send } from '../../lib/request.js';
import { formatDateTime } from '../../lib/format.js';
import { ConfirmDialog } from '../../ui/ConfirmDialog.js';
import { LoadError } from '../../ui/ScreenState.js';

const PLANNER_STATUS = { active: 'подключён', expired: 'нужно переподключить', revoked: 'отключён' };

function SwitchRow({ id, title, hint, checked, busy, onToggle }) {
  return html`
    <li class="row">
      <div class="row-main">
        <span id=${id} class="row-title">${title}</span>
        ${hint ? html`<span class="field-hint">${hint}</span>` : null}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked=${checked ? 'true' : 'false'}
        aria-labelledby=${id}
        class="switch"
        disabled=${busy}
        onClick=${onToggle}
      >
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </button>
    </li>
  `;
}

export function IntegrationsCard() {
  const { data, failed, reload, replace } = useResource('/api/settings', 'settings');
  const [busy, setBusy] = useState(false);
  // The «Не читать заметки» confirmation: turning notes off deletes
  // everything read from them.
  const [confirmOff, setConfirmOff] = useState(null);

  async function write(path, on) {
    setBusy(true);
    const res = await send(path, { on });
    setBusy(false);
    if (res.ok && res.data && res.data.settings) {
      replace(res.data.settings);
      pushToast('Сохранено.');
      return;
    }
    if (res.status === 409) pushToast('Планер ещё не подключён — /planner_link в Telegram.');
    else if (res.status !== 401) pushToast(res.error);
  }

  if (!data) return failed ? html`<${LoadError} onRetry=${reload} />` : null;
  const { vault, planner, claude } = data;
  if (!vault && !planner && !claude) return null;

  return html`
    <section class="card" aria-labelledby="integrations-heading">
      <h2 id="integrations-heading">Подключения</h2>
      <ul class="card-list">
        ${vault
          ? html`<${SwitchRow}
              id="notes-switch-label"
              title="Заметки Obsidian"
              hint=${vault.notes_consent
                ? 'Echo читает заметки с меткой anchor: personal или anchor: knowledge.'
                : 'Выключено: заметки не читаются.'}
              checked=${vault.notes_consent}
              busy=${busy}
              onToggle=${(e) =>
                vault.notes_consent
                  ? setConfirmOff({ trigger: e.currentTarget })
                  : write('/api/settings/notes', true)}
            />`
          : null}
        ${planner
          ? planner.linked
            ? html`<${SwitchRow}
                id="planner-switch-label"
                title="Планер"
                hint=${`${PLANNER_STATUS[planner.status] || planner.status}${planner.enabled ? '' : ' · синхронизация на паузе'}`}
                checked=${planner.enabled}
                busy=${busy}
                onToggle=${() => write('/api/settings/planner', !planner.enabled)}
              />`
            : html`
                <li class="row">
                  <div class="row-main">
                    <span class="row-title">Планер</span>
                    <span class="field-hint">Не подключён. Подключить — /planner_link в Telegram.</span>
                  </div>
                </li>
              `
          : null}
        ${claude
          ? html`
              <li class="row">
                <div class="row-main">
                  <span class="row-title">Claude</span>
                  <span class="field-hint">
                    ${claude.connected
                      ? `Подключён до ${formatDateTime(claude.expires_at)} · библиотека: ${claude.library_read ? 'вкл' : 'выкл'}, запись: ${claude.library_write ? 'вкл' : 'выкл'}.`
                      : 'Не подключён.'}
                    ${' '}Переключается в Telegram (/claude).
                  </span>
                </div>
              </li>
            `
          : null}
      </ul>
      <${ConfirmDialog}
        target=${confirmOff}
        busy=${busy}
        heading="Перестать читать заметки?"
        confirmLabel="Выключить"
        fallbackFocusId="integrations-heading"
        onClose=${() => setConfirmOff(null)}
        onConfirm=${() => write('/api/settings/notes', false)}
      >
        <p>Всё, что Echo прочитал из заметок, будет удалено. Сами заметки в Obsidian не тронуты — включишь снова, и он прочитает их заново.</p>
      <//>
    </section>
  `;
}

function UndoRow({ run, onUndo }) {
  const [busy, setBusy] = useState(false);
  const mounted = useMountedRef();
  async function undo() {
    setBusy(true);
    await onUndo(run.id);
    if (mounted.current) setBusy(false);
  }
  return html`
    <li class="row">
      <div class="row-main">
        <p class="field-value">${run.label}</p>
        <span class="field-hint mono">${formatDateTime(run.created_at)}</span>
      </div>
      <button type="button" class="btn btn-ghost" disabled=${busy} onClick=${undo}>Отменить</button>
    </li>
  `;
}

export function DigestCard() {
  const [window_, setWindow] = useState('24h');
  const { data, failed, reload } = useResource(`/api/digest?window=${window_}`, 'settings', {
    accept: (d) => Array.isArray(d.lines),
  });

  async function undo(id) {
    const res = await send(`/api/digest/${id}/undo`, {}, { fallback: 'Не удалось отменить.' });
    if (res.ok) {
      pushToast(res.data && res.data.skipped_conflicts ? 'Часть изменений уже перезаписана.' : 'Отменено.');
      return;
    }
    if (res.status === 409) {
      pushToast('Отменить нельзя.');
      reload();
    } else if (res.status !== 401) pushToast(res.error);
  }

  // The window is part of the path; useResource refetches on a new one.
  function choose(w) {
    setWindow(w);
  }

  let body;
  if (!data || data.window !== window_) body = failed ? html`<${LoadError} onRetry=${reload} />` : html`<div aria-busy="true"></div>`;
  else {
    // /digest's first line is its header («Фоновая работа за 24 ч —
    // $0.12», or «Фоновой работы не было.»); the rest are its bullets.
    const [header, ...items] = data.lines;
    body = html`
      <p class="field-value">${header}</p>
      ${items.length
        ? html`
            <ul class="plain-list digest-lines">
              ${items.map((line, i) => html`<li key=${i}>${line.replace(/^• /, '')}</li>`)}
            </ul>
          `
        : null}
      ${data.undoable.length
        ? html`
            <h3 class="subheading">Можно отменить</h3>
            <ul class="card-list">${data.undoable.map((run) => html`<${UndoRow} key=${run.id} run=${run} onUndo=${undo} />`)}</ul>
          `
        : null}
    `;
  }

  return html`
    <section class="card" aria-labelledby="digest-heading">
      <div class="card-row">
        <h2 id="digest-heading">Фоновая работа</h2>
        <div class="segmented digest-window" role="group" aria-label="Период">
          <button type="button" aria-pressed=${window_ === '24h' ? 'true' : 'false'} onClick=${() => choose('24h')}>24 ч</button>
          <button type="button" aria-pressed=${window_ === '7d' ? 'true' : 'false'} onClick=${() => choose('7d')}>7 дней</button>
        </div>
      </div>
      <p class="field-hint">Что Echo делал сам, пока ты не писал. То же, что /digest.</p>
      ${body}
    </section>
  `;
}
