// Full-screen PIN login. Resolves true when logged in, false when cancelled.

import { h, icon, setText, clearToasts } from './dom.js';
import { session } from './session.js';

const MAX_LEN = 12;

export function showLogin({ reason = '' } = {}) {
  return new Promise((resolve) => {
    let pin = '';
    let busy = false;
    let lockTimer = null;
    let done = false;

    const dots = h('div', { class: 'pin-dots' });
    const msg = h('div', { class: 'login-msg' });
    const keys = h('div', { class: 'keypad pin' });
    const cancelBtn = h('button', { class: 'btn', type: 'button', onclick: () => finish(false) }, icon('close'), 'Cancel');

    const root = h('div', { class: 'kb numeric login' },
      h('div', { class: 'kb-left' },
        h('div', { class: 'login-head' },
          h('span', { class: 'login-ic' }, icon('lock')),
          h('div', null, h('div', { class: 'login-title' }, 'Admin login'),
            h('div', { class: 'login-sub' }, reason || 'Enter the PIN to change network settings.'))),
        dots, msg,
        h('div', { class: 'bottom' }, cancelBtn)),
      keys);

    function setMsg(text, kind = '') {
      setText(msg, text);
      msg.className = `login-msg ${kind}`.trim();
    }

    function render() {
      dots.replaceChildren();
      const n = Math.max(pin.length, 6);
      for (let i = 0; i < n; i++) dots.append(h('span', { class: i < pin.length ? 'on' : null }));
      for (const b of keys.querySelectorAll('button')) b.disabled = busy || !!lockTimer || !session.configured;
    }

    function press(d) {
      if (busy || lockTimer || pin.length >= MAX_LEN) return;
      pin += d;
      if (msg.classList.contains('bad')) setMsg('');
      render();
    }
    function backspace() {
      if (busy || lockTimer) return;
      pin = pin.slice(0, -1);
      render();
    }

    function startLock(seconds) {
      let left = seconds;
      const upd = () => setMsg(`Too many wrong PINs. Try again in ${left} s.`, 'bad');
      upd();
      lockTimer = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          clearInterval(lockTimer);
          lockTimer = null;
          setMsg('');
          render();
        } else upd();
      }, 1000);
      render();
    }

    async function submit() {
      if (busy || lockTimer) return;
      if (pin.length < 4) {
        setMsg('Enter your PIN.', 'bad');
        return;
      }
      busy = true;
      setMsg('Checking…');
      render();
      try {
        await session.login(pin);
        finish(true);
      } catch (e) {
        busy = false;
        pin = '';
        if (e.retryAfter) startLock(e.retryAfter);
        else setMsg(e.message, 'bad');
        render();
      }
    }

    function finish(ok) {
      if (done) return;
      done = true;
      clearInterval(lockTimer);
      document.removeEventListener('keydown', onKey, true);
      window.removeEventListener('netpanel-reset', onReset);
      root.remove();
      resolve(ok);
    }

    const onReset = () => finish(false);
    function onKey(e) {
      if (/^[0-9]$/.test(e.key)) press(e.key);
      else if (e.key === 'Backspace') backspace();
      else if (e.key === 'Enter') submit();
      else if (e.key === 'Escape') finish(false);
      else return;
      e.preventDefault();
      e.stopPropagation();
    }

    const key = (label, fn, cls = 'key') => h('button', { class: cls, type: 'button', onclick: fn }, label);
    for (const d of '123456789') keys.append(key(d, () => press(d)));
    const bs = key(icon('backspace'), backspace, 'key fn');
    bs.setAttribute('aria-label', 'Backspace');
    keys.append(bs, key('0', () => press('0')), key(h('span', null, icon('check')), submit, 'key done'));

    document.addEventListener('keydown', onKey, true);
    window.addEventListener('netpanel-reset', onReset);
    clearToasts();
    document.getElementById('layer').append(root);
    render();

    // Up-to-date info: PIN configured? still locked? demo hint?
    session.info().then((r) => {
      if (done) return;
      if (!r.configured) setMsg('No PIN is set yet. Set one over SSH:  sudo netpanel-passwd', 'bad');
      else if (r.locked_for) startLock(r.locked_for);
      else if (r.demo) setMsg('Demo mode - PIN is 123456');
      render();
    }).catch((e) => setMsg(e.message, 'bad'));
  });
}
