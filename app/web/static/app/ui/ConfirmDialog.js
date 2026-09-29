// A native modal <dialog> asking to confirm one destructive action.
// Generalised from Memory's «Забыть» dialog: one instance per screen,
// opened by setting `target` (any value, plus the `trigger` element
// focus returns to) and closed by every path the dialog itself offers
// -- Отмена, Escape (the UA fires 'cancel' then 'close'), and the
// confirm button once `onConfirm` resolves. `onClose` must be
// idempotent: the native 'close' event is the one place it is called.
//
// Focus returns to `target.trigger` when it is still in the document,
// else to the element with id `fallbackFocusId` (a confirmed delete
// usually removes the trigger's own row).
import { html } from '../html.js';
import { useEffect, useRef } from '../../vendor/hooks.module.js';

export function ConfirmDialog({
  target,
  busy,
  heading,
  confirmLabel,
  fallbackFocusId,
  onClose,
  onConfirm,
  children,
}) {
  const dialogRef = useRef(null);
  const cancelButtonRef = useRef(null);
  // The most recent non-null `target`: by the time the native 'close'
  // event runs, the parent may already have cleared it.
  const lastTargetRef = useRef(null);

  useEffect(() => {
    if (target) lastTargetRef.current = target;
    const dialog = dialogRef.current;
    if (dialog && target && !dialog.open) {
      dialog.showModal();
      if (cancelButtonRef.current) cancelButtonRef.current.focus();
    }
  }, [target]);

  function requestClose() {
    if (dialogRef.current && dialogRef.current.open) dialogRef.current.close();
  }

  function onNativeClose() {
    const trigger = lastTargetRef.current && lastTargetRef.current.trigger;
    onClose();
    if (trigger && typeof trigger.focus === 'function' && trigger.isConnected) {
      trigger.focus();
    } else if (fallbackFocusId) {
      const fallback = document.getElementById(fallbackFocusId);
      if (fallback) fallback.focus();
    }
  }

  async function handleConfirm() {
    await onConfirm();
    requestClose();
  }

  return html`
    <dialog ref=${dialogRef} class="confirm-dialog" aria-labelledby="confirm-heading" onClose=${onNativeClose}>
      ${target
        ? html`
            <h2 id="confirm-heading">${heading}</h2>
            ${children}
            <div class="btn-row">
              <button type="button" ref=${cancelButtonRef} class="btn btn-ghost" disabled=${busy} onClick=${requestClose}>
                Отмена
              </button>
              <button type="button" class="btn btn-primary btn-danger" disabled=${busy} onClick=${handleConfirm}>
                ${confirmLabel}
              </button>
            </div>
          `
        : null}
    </dialog>
  `;
}
