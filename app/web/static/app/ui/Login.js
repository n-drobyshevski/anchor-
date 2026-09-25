// The two-step login screen: passphrase, then a Telegram one-time
// code. Ported 1:1 from the old app.js's login flow -- same ids and
// classes (app.css is unchanged for this section), same status-code
// handling, same focus/clear-on-step-change behavior -- with `hidden`
// now a prop bound to `stage` instead of a DOM toggle, and the two
// inputs read through refs instead of by id.
import { html } from '../html.js';
import { useEffect, useRef, useState } from '../../vendor/hooks.module.js';
import { apiPost } from '../api.js';
import { auth } from '../store.js';

function minutesText(retryAfterSeconds) {
  const m = Math.max(1, Math.ceil((retryAfterSeconds || 60) / 60));
  return `Слишком много попыток — попробуй через ${m} мин.`;
}

export function Login({ stage }) {
  const passphraseRef = useRef(null);
  const codeRef = useRef(null);
  const [passphraseError, setPassphraseError] = useState('');
  const [codeError, setCodeError] = useState('');
  const [passphraseBusy, setPassphraseBusy] = useState(false);
  const [codeBusy, setCodeBusy] = useState(false);

  // Mirrors the old setLoginStage(): whichever step becomes visible
  // gets its error cleared, its field blanked, and focus -- on every
  // stage change, including the very first render (server-supplied
  // `stage`, e.g. a pending code from a previous page load).
  useEffect(() => {
    if (stage === 'code') {
      setCodeError('');
      if (codeRef.current) {
        codeRef.current.value = '';
        codeRef.current.focus();
      }
    } else {
      setPassphraseError('');
      if (passphraseRef.current) {
        passphraseRef.current.value = '';
        passphraseRef.current.focus();
      }
    }
  }, [stage]);

  async function onPassphraseSubmit(e) {
    e.preventDefault();
    setPassphraseError('');
    const value = passphraseRef.current.value;
    if (!value) return;
    setPassphraseBusy(true);
    const res = await apiPost('/api/auth/passphrase', { passphrase: value });
    setPassphraseBusy(false);
    if (res.status === 200) {
      auth.value = 'code';
    } else if (res.status === 401) {
      setPassphraseError('Неверный пароль или код.');
    } else if (res.status === 429) {
      setPassphraseError(minutesText(res.data && res.data.retry_after));
    } else {
      setPassphraseError('Что-то пошло не так. Попробуй ещё раз.');
    }
  }

  async function onCodeSubmit(e) {
    e.preventDefault();
    setCodeError('');
    const value = codeRef.current.value;
    if (!value) return;
    setCodeBusy(true);
    const res = await apiPost('/api/auth/code', { code: value });
    setCodeBusy(false);
    if (res.status === 200) {
      auth.value = 'in';
    } else if (res.status === 401) {
      setCodeError('Неверный пароль или код.');
    } else if (res.status === 429) {
      setCodeError(minutesText(res.data && res.data.retry_after));
    } else {
      setCodeError('Что-то пошло не так. Попробуй ещё раз.');
    }
  }

  return html`
    <section id="login">
      <form
        id="login-passphrase"
        class="login-card"
        autocomplete="on"
        hidden=${stage === 'code'}
        onSubmit=${onPassphraseSubmit}
      >
        <h1>Anchor</h1>
        <p class="login-hint">Введи пароль.</p>
        <label for="passphrase-input" class="sr-only">Пароль</label>
        <input
          id="passphrase-input"
          ref=${passphraseRef}
          name="passphrase"
          type="password"
          autocomplete="current-password"
          required
          minlength="1"
          maxlength="4000"
        />
        <button type="submit" class="btn btn-primary btn-block" disabled=${passphraseBusy}>Войти</button>
        <p id="passphrase-error" class="login-error" role="alert" hidden=${!passphraseError}>
          ${passphraseError}
        </p>
      </form>

      <form
        id="login-code"
        class="login-card"
        autocomplete="on"
        hidden=${stage !== 'code'}
        onSubmit=${onCodeSubmit}
      >
        <h1>Anchor</h1>
        <p class="login-hint">Код отправлен в Telegram.</p>
        <label for="code-input" class="sr-only">Код</label>
        <input
          id="code-input"
          ref=${codeRef}
          name="code"
          type="text"
          inputmode="text"
          autocomplete="one-time-code"
          autocapitalize="characters"
          autocorrect="off"
          spellcheck="false"
          required
          maxlength="9"
        />
        <button type="submit" class="btn btn-primary btn-block" disabled=${codeBusy}>Подтвердить</button>
        <button type="button" id="code-back" class="link-button" onClick=${() => { auth.value = 'none'; }}>
          Назад
        </button>
        <p id="code-error" class="login-error" role="alert" hidden=${!codeError}>${codeError}</p>
      </form>
    </section>
  `;
}
