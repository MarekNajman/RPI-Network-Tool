// Full-screen on-screen keyboard. There is no system OSK in the kiosk, so every
// text field opens this editor. Numeric kinds get a big keypad, text kinds QWERTY.

import { h, icon, clearToasts } from './dom.js';
import { V } from './validate.js';

const NUMERIC = new Set(['ip', 'mask', 'prefix', 'cidr', 'iplist', 'number']);
const VALIDATOR = { ip: V.ip, mask: V.mask, prefix: V.prefix, cidr: V.cidr, iplist: V.iplist };
const HINTS = {
  ip: 'IPv4 address, e.g. 192.168.1.10',
  mask: 'Subnet mask (255.255.255.0) or prefix length (24)',
  prefix: 'Prefix length 1-32 (24 = 255.255.255.0)',
  cidr: 'Address/prefix, e.g. 10.42.0.1/24',
  iplist: 'Up to 3 addresses separated by commas',
  number: 'Number',
  text: '',
  password: '',
};

const LETTERS = ['qwertyuiop', 'asdfghjkl', 'zxcvbnm'];
const SYM1 = ['1234567890', '@#$%&*-+()', '!"\':;/?'];
const SYM2 = ['[]{}<>^~`|', '\\=-_+#$%&*', '!"\':;/?'];

export function openInput({
  label = '',
  kind = 'text',
  value = '',
  optional = false,
  presets = [],
  validate = null,
  hint = '',
  reveal = false,
  maxLength = 64,
} = {}) {
  return new Promise((resolve) => {
    const numeric = NUMERIC.has(kind);
    const secret = kind === 'password';
    const check = validate || VALIDATOR[kind] || (() => '');
    let val = String(value ?? '');
    let shown = !secret || reveal;
    let touched = false; // user typed something
    let attempted = false; // user pressed Done with an invalid value -> show red
    // An existing value starts "selected": typing replaces it, backspace edits it.
    let selected = val.length > 0;
    let page = 'abc'; // abc | sym1 | sym2
    let shift = false;
    let caps = false;
    let lastShiftTap = 0;

    // ---------------------------------------------------------------- display
    const valEl = h('div', { class: 'val' });
    const eyeBtn = secret
      ? h('button', { class: 'eye', type: 'button', onclick: () => { shown = !shown; render(); } })
      : null;
    const display = h('div', { class: 'kb-display' }, valEl, eyeBtn);
    valEl.addEventListener('click', () => { if (selected) { selected = false; render(); } });
    const hintEl = h('div', { class: 'kb-hint' });

    const errorOf = (v) => (optional && !v.trim() ? '' : check(secret ? v : v.trim()));

    function render() {
      valEl.replaceChildren();
      if (val) valEl.append(h('span', { class: `t${selected ? ' sel' : ''}` }, secret && !shown ? '•'.repeat(val.length) : val));
      valEl.append(h('span', { class: 'caret' }));
      valEl.classList.toggle('ph', !val);
      valEl.scrollLeft = valEl.scrollWidth;
      if (eyeBtn) eyeBtn.replaceChildren(icon(shown ? 'eye-off' : 'eye'));
      const err = errorOf(val);
      const showErr = attempted && !!err;
      hintEl.textContent = err && (touched || attempted) ? err : hint || HINTS[kind] || '';
      hintEl.classList.toggle('bad', showErr);
      display.classList.toggle('bad', showErr);
    }

    // ---------------------------------------------------------------- editing
    function insert(ch) {
      if (val.length >= maxLength) return;
      if (numeric) {
        if (kind === 'prefix' && ch === '/' ) return;
        if (!/[0-9./,]/.test(ch)) return;
      }
      if (selected) val = '';
      selected = false;
      val += ch;
      touched = true;
      if (shift && !caps) {
        shift = false;
        buildKeys();
      }
      render();
    }
    function backspace() {
      selected = false;
      if (!val) return;
      val = val.slice(0, -1);
      touched = true;
      render();
    }
    function clearAll() {
      selected = false;
      val = '';
      touched = true;
      render();
    }
    function finish(result) {
      document.removeEventListener('keydown', onKey, true);
      root.remove();
      resolve(result);
    }
    function done() {
      if (errorOf(val)) {
        attempted = true;
        render();
        return;
      }
      finish(secret ? val : val.trim());
    }

    // Backspace: delete once on touch, then auto-repeat while held.
    function backspaceKey(cls = 'key fn w15') {
      let t1 = null;
      let t2 = null;
      const stop = () => {
        clearTimeout(t1);
        clearInterval(t2);
      };
      const b = h('button', { class: cls, type: 'button', 'aria-label': 'Backspace' }, icon('backspace'));
      b.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        backspace();
        stop();
        t1 = setTimeout(() => { t2 = setInterval(backspace, 70); }, 450);
      });
      for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) b.addEventListener(ev, stop);
      return b;
    }

    const key = (label, onTap, cls = 'key') => h('button', { class: cls, type: 'button', onclick: onTap }, label);
    const charKey = (ch, cls = 'key') => key(ch, () => insert(ch), cls);
    const doneKey = (cls) => key('Done', done, cls);

    // ---------------------------------------------------------------- layouts
    const keysEl = h('div', numeric ? { class: 'keypad' } : { class: 'kb-keys' });

    function buildKeys() {
      keysEl.replaceChildren();
      if (numeric) {
        const extra = kind === 'cidr' ? '/' : kind === 'iplist' ? ',' : '';
        const dotOk = !['prefix', 'number'].includes(kind);
        for (const ch of '789') keysEl.append(charKey(ch));
        keysEl.append(backspaceKey('key fn'));
        for (const ch of '456') keysEl.append(charKey(ch));
        const dot = charKey('.');
        dot.disabled = !dotOk;
        keysEl.append(dot);
        for (const ch of '123') keysEl.append(charKey(ch));
        const ex = charKey(extra || ' ');
        if (!extra) ex.disabled = true;
        keysEl.append(ex);
        keysEl.append(key('Clear', clearAll, 'key fn'), charKey('0'), doneKey('key done'));
        render();
        return;
      }

      const rows = page === 'abc' ? LETTERS : page === 'sym1' ? SYM1 : SYM2;
      const upper = shift || caps;
      const cased = (s) => (page === 'abc' && upper ? s.toUpperCase() : s);

      const r1 = h('div', { class: 'kb-row' }, [...rows[0]].map((c) => charKey(cased(c))));
      const r2 = h('div', { class: 'kb-row' }, [...rows[1]].map((c) => charKey(cased(c))));

      let lead;
      if (page === 'abc') {
        lead = key(icon('shift'), () => {
          const now = Date.now();
          if (caps) { caps = false; shift = false; }
          else if (shift && now - lastShiftTap < 400) caps = true;
          else shift = !shift;
          lastShiftTap = now;
          buildKeys();
        }, `key fn w15${caps ? ' lock' : shift ? ' on' : ''}`);
      } else {
        lead = key(page === 'sym1' ? '#+=' : '123', () => { page = page === 'sym1' ? 'sym2' : 'sym1'; buildKeys(); }, 'key fn w15');
      }
      const r3 = h('div', { class: 'kb-row' }, lead, [...rows[2]].map((c) => charKey(cased(c))), backspaceKey());

      const toggle = key(page === 'abc' ? '?123' : 'ABC', () => { page = page === 'abc' ? 'sym1' : 'abc'; buildKeys(); }, 'key fn w15');
      const r4 = h('div', { class: 'kb-row' },
        toggle,
        charKey(page === 'abc' ? '-' : ','),
        key('space', () => insert(' '), 'key fn space'),
        charKey('.'),
        charKey(page === 'abc' ? '_' : '@'),
        doneKey('key done'));
      keysEl.append(r1, r2, r3, r4);
      render();
    }

    // ---------------------------------------------------------------- shell
    let root;
    const cancelBtn = h('button', { class: 'btn', type: 'button', onclick: () => finish(null) }, icon('close'), 'Cancel');
    if (numeric) {
      const presetEls = presets.length
        ? h('div', { class: 'presets' }, presets.map((p) => {
            const pv = typeof p === 'string' ? p : p.value;
            const pl = typeof p === 'string' ? p : p.label;
            return h('button', {
              class: 'btn', type: 'button',
              onclick: () => {
                if (kind === 'iplist' && val.trim()) {
                  const items = val.split(/[\s,]+/).filter(Boolean);
                  if (!items.includes(pv)) val = [...items, pv].join(', ');
                } else val = pv;
                selected = false;
                touched = true;
                render();
              },
            }, pl);
          }))
        : null;
      root = h('div', { class: 'kb numeric' },
        h('div', { class: 'kb-left' },
          h('div', { class: 'kb-top' }, h('div', { class: 'lbl' }, label)),
          display, hintEl, presetEls,
          h('div', { class: 'bottom' }, cancelBtn)),
        keysEl);
    } else {
      const doneTop = h('button', { class: 'btn primary', type: 'button', onclick: done }, icon('check'), 'Done');
      root = h('div', { class: 'kb' },
        h('div', { class: 'kb-top' }, cancelBtn, h('div', { class: 'lbl' }, label), doneTop),
        display, hintEl, keysEl);
    }

    // Physical keyboard support (handy for maintenance with a USB keyboard).
    function onKey(e) {
      if (e.key === 'Enter') done();
      else if (e.key === 'Escape') finish(null);
      else if (e.key === 'Backspace') backspace();
      else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) insert(e.key);
      else return;
      e.preventDefault();
      e.stopPropagation();
    }
    document.addEventListener('keydown', onKey, true);

    buildKeys();
    clearToasts();
    document.getElementById('layer').append(root);
    render();
  });
}
