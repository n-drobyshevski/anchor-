// State for a "show value / Изменить -> textarea / Сохранить" field,
// shared by State's action card and Memory's cards: the draft, busy
// and error state, focus moving into the textarea on open and back to
// the button that opened it on close (instead of falling to <body>),
// and the keys -- Escape cancels, Ctrl/Cmd+Enter saves (plain Enter is
// a newline in a multi-line field).
//
// `onSave(draft)` returns true (saved: close), a string (show it), or
// anything else, which save() hands back to the caller untouched (e.g.
// Memory's {duplicate}).
import { useEffect, useRef, useState } from '../../vendor/hooks.module.js';

export function useInlineEdit(current, onSave) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(current);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const textareaRef = useRef(null);
  const openerRef = useRef(null);
  const wasEditingRef = useRef(false);

  useEffect(() => {
    if (editing && textareaRef.current) textareaRef.current.focus();
    if (!editing && wasEditingRef.current && openerRef.current) openerRef.current.focus();
    wasEditingRef.current = editing;
  }, [editing]);

  function start() {
    setDraft(current);
    setError('');
    setEditing(true);
  }

  function cancel() {
    setDraft(current);
    setError('');
    setEditing(false);
  }

  async function save() {
    setBusy(true);
    setError('');
    const result = await onSave(draft);
    setBusy(false);
    if (result === true) setEditing(false);
    else if (typeof result === 'string') setError(result);
    return result;
  }

  function onKeyDown(e, saveFn = save) {
    if (e.key === 'Escape') {
      e.preventDefault();
      cancel();
      return;
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!busy) saveFn();
    }
  }

  return { editing, draft, setDraft, error, setError, busy, start, cancel, save, onKeyDown, textareaRef, openerRef };
}
