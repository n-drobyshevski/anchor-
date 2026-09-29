// Память's «Блокнот» tab: Echo's notebook (GET /api/notebook) -- the
// same entries Telegram's /mind lists. Намерения are the user's (and
// the weekly review's); Наблюдения and Незакрытое are Echo's own
// working notes. The user can add an intention (`/mind add`) and close
// any entry, Echo's included (/mind's ✖).
import { html } from '../../html.js';
import { useRef, useState } from '../../../vendor/hooks.module.js';
import { pushToast } from '../../store.js';
import { useResource } from '../../hooks.js';
import { send } from '../../lib/request.js';
import { CharCounter } from '../../ui/CharCounter.js';
import { ConfirmDialog } from '../../ui/ConfirmDialog.js';
import { LoadError } from '../../ui/ScreenState.js';

const SOURCE_LABELS = { user: 'от тебя', anchor: 'Echo', review: 'недельный обзор' };

const GROUPS = [
  { key: 'intentions', title: 'Намерения', empty: 'Намерений пока нет.' },
  { key: 'observations', title: 'Наблюдения', empty: 'Echo пока ничего не заметил.' },
  { key: 'threads', title: 'Незакрытое', empty: 'Незакрытых тем нет.' },
];

function AddIntention({ textMax, full, onAdd }) {
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const trimmed = text.trim();

  async function submit(e) {
    e.preventDefault();
    if (!trimmed || busy) return;
    setBusy(true);
    setError('');
    const result = await onAdd(trimmed);
    setBusy(false);
    if (result === true) setText('');
    else setError(result);
  }

  return html`
    <form class="inline-add" onSubmit=${submit}>
      <label for="notebook-add" class="sr-only">Новое намерение</label>
      <input
        id="notebook-add"
        type="text"
        placeholder=${full ? 'Сначала закрой одно из намерений' : 'Новое намерение'}
        maxlength=${textMax}
        disabled=${busy || full}
        value=${text}
        onInput=${(e) => setText(e.target.value)}
      />
      <button type="submit" class="btn btn-primary" disabled=${busy || full || !trimmed}>Добавить</button>
      ${text.length > textMax * 0.8 ? html`<${CharCounter} length=${trimmed.length} max=${textMax} />` : null}
      ${error ? html`<p class="inline-error" role="alert">${error}</p>` : null}
    </form>
  `;
}

export function NotebookTab() {
  const { data, failed, reload } = useResource('/api/notebook', 'notebook');
  // {item, trigger} | null -- the pending «Закрыть» confirmation.
  const [target, setTarget] = useState(null);
  const [busy, setBusy] = useState(false);

  async function add(text) {
    const res = await send('/api/notebook', { text });
    if (res.ok) {
      pushToast('Записал.');
      return true;
    }
    return (res.data && res.data.message) || res.error;
  }

  async function confirmClose() {
    if (!target) return;
    setBusy(true);
    const res = await send(`/api/notebook/${target.item.id}/close`, {}, { fallback: 'Не удалось сохранить.' });
    setBusy(false);
    if (res.ok) pushToast('Закрыто.');
    else if (res.status === 404) {
      pushToast('Уже неактуально');
      reload();
    } else if (res.status !== 401) pushToast(res.error);
  }

  if (!data) return failed ? html`<${LoadError} onRetry=${reload} />` : html`<div aria-busy="true"></div>`;

  const full = data.intentions.length >= data.limits.intentions_max;

  return html`
    <div class="memory-tab">
      <p class="field-hint">Рабочие заметки Echo о тебе — то же, что /mind в Telegram. Закрыть можно любую.</p>
      ${GROUPS.map(
        (group) => html`
          <section class="card" key=${group.key} aria-labelledby=${`notebook-${group.key}`}>
            <div class="card-row">
              <h2 id=${`notebook-${group.key}`}>${group.title}</h2>
              ${group.key === 'intentions'
                ? html`<span class="field-hint"><span class="mono">${data.intentions.length}</span> из <span class="mono">${data.limits.intentions_max}</span></span>`
                : null}
            </div>
            ${data[group.key].length
              ? html`
                  <ul class="card-list">
                    ${data[group.key].map((item) => html`<${EntryRow} key=${item.id} item=${item} onClose=${setTarget} />`)}
                  </ul>
                `
              : html`<p class="field-hint">${group.empty}</p>`}
            ${group.key === 'intentions'
              ? html`<${AddIntention} textMax=${data.limits.text_max} full=${full} onAdd=${add} />`
              : null}
          </section>
        `,
      )}
      <${ConfirmDialog}
        target=${target}
        busy=${busy}
        heading="Закрыть запись?"
        confirmLabel="Закрыть"
        fallbackFocusId="memory-tab-notebook"
        onClose=${() => setTarget(null)}
        onConfirm=${confirmClose}
      >
        ${target ? html`<p class="field-hint">«${target.item.text}»</p>` : null}
        <p>Echo перестанет на неё опираться.</p>
      <//>
    </div>
  `;
}

function EntryRow({ item, onClose }) {
  const ref = useRef(null);
  return html`
    <li class="row">
      <div class="row-main">
        <p class="field-value">${item.text}</p>
        <span class="field-hint">${SOURCE_LABELS[item.source] || item.source}</span>
      </div>
      <button type="button" ref=${ref} class="btn btn-ghost" onClick=${() => onClose({ item, trigger: ref.current })}>
        Закрыть
      </button>
    </li>
  `;
}
