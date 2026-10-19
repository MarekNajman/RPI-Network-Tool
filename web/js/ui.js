// Overlays (confirm, error, busy, toast, chooser) and form widgets (Field, Segmented).
// Everything is rendered inside the single kiosk window - no native dialogs.

import { h, icon, setText, clearToasts } from './dom.js';
import { openInput } from './keyboard.js';

const layer = () => document.getElementById('layer');

// ------------------------------------------------------------------ modals

function modal({ cls = '', iconName = 'info', title, text, code, extra, actions }) {
  clearToasts();
  const box = h('div', { class: `modal ${cls}` },
    h('div', { class: 'modal-head' }, icon(iconName), h('h2', null, title)),
    h('div', { class: 'body-scroll' },
      text ? h('p', null, text) : null,
      code ? h('div', { class: 'code' }, code) : null,
      extra || null),
    h('div', { class: 'actions' }, actions));
  const ov = h('div', { class: 'overlay' }, box);
  layer().append(ov);
  return ov;
}

export function confirm({ title, text, code, extra, ok = 'Apply', cancel = 'Cancel', danger = false, iconName, okClass }) {
  return new Promise((resolve) => {
    let ov;
    const close = (v) => { ov.remove(); resolve(v); };
    ov = modal({
      iconName: iconName || (danger ? 'warn' : 'info'),
      title, text, code, extra,
      actions: [
        h('button', { class: 'btn', type: 'button', onclick: () => close(false) }, cancel),
        h('button', { class: `btn ${okClass || (danger ? 'danger' : 'primary')}`, type: 'button', onclick: () => close(true) }, ok),
      ],
    });
  });
}

export function alertError(title, text) {
  return new Promise((resolve) => {
    let ov;
    ov = modal({
      cls: 'error', iconName: 'warn', title, text,
      actions: [h('button', { class: 'btn primary', type: 'button', onclick: () => { ov.remove(); resolve(); } }, 'OK')],
    });
  });
}

export function busy(text, sub = '') {
  clearToasts();
  const t = h('div', null, text);
  const s = h('small', null, sub);
  const ov = h('div', { class: 'overlay busy' }, h('div', { class: 'spinner' }), t, s);
  layer().append(ov);
  return { close: () => ov.remove(), set: (txt) => setText(t, txt) };
}

let toastTimer = null;
export function toast(text, kind = 'ok') {
  clearToasts();
  clearTimeout(toastTimer);
  const el = h('div', { class: `toast ${kind}` }, icon(kind === 'err' ? 'warn' : kind === 'info' ? 'info' : 'check'), text);
  document.body.append(el);
  toastTimer = setTimeout(() => el.remove(), kind === 'err' ? 6000 : 3800);
}

// options: [{value, label}], resolves with chosen value or null
export function choose({ title, options, value, cols = 4 }) {
  return new Promise((resolve) => {
    let ov;
    const close = (v) => { ov.remove(); resolve(v); };
    const grid = h('div', { class: 'choices' });
    grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    for (const o of options) {
      grid.append(h('button', {
        class: `btn${o.value === value ? ' on' : ''}`, type: 'button', onclick: () => close(o.value),
      }, o.label));
    }
    ov = modal({
      iconName: 'radio', title, extra: grid,
      actions: [h('button', { class: 'btn', type: 'button', onclick: () => close(null) }, 'Cancel')],
    });
  });
}

// Runs an async action behind a busy overlay; shows toast or error dialog.
export async function runAction({ busyText, busySub = '', fn, errorTitle = 'Operation failed', onFieldErrors }) {
  const b = busy(busyText, busySub);
  try {
    const res = await fn();
    b.close();
    if (res && res.message) toast(res.message, 'ok');
    return res || {};
  } catch (e) {
    b.close();
    if (e.authRequired) return null; // session expired: the app already logged out
    if (e.errors && Object.keys(e.errors).length && onFieldErrors) onFieldErrors(e.errors);
    await alertError(errorTitle, e.message || String(e));
    return null;
  }
}

// ------------------------------------------------------------------ widgets

export class Field {
  // kind: ip | mask | prefix | cidr | iplist | number | text | password | choice
  constructor({ label, kind = 'text', value = '', placeholder = '', optional = false, presets, validate,
    hint, onChange, onTap, reveal = false, tag = '' }) {
    Object.assign(this, { label, kind, placeholder, optional, presets, validate, hint, onChange, onTap });
    this._value = value || '';
    this.readonly = false;
    this.revealed = reveal;
    this.valEl = h('span', { class: 'v' });
    this.tagEl = h('em', null, tag);
    this.msgEl = h('div', { class: 'msg hidden' });
    this.box = h('div', { class: 'input', role: 'button', tabindex: '0' }, this.valEl);
    if (kind === 'password') {
      this.eye = h('button', { class: 'eye', type: 'button' });
      this.eye.addEventListener('click', (e) => { e.stopPropagation(); this.revealed = !this.revealed; this.render(); });
      this.box.append(this.eye);
    } else if (kind === 'choice') {
      this.box.append(icon('chevron', 'tail'));
    }
    this.box.addEventListener('click', () => this.tap());
    this.el = h('div', { class: 'field' }, h('label', null, label, this.tagEl), this.box, this.msgEl);
    this.render();
  }

  get value() { return this._value; }

  set value(v) { this._value = v == null ? '' : String(v); this.render(); }

  setTag(t) { setText(this.tagEl, t || ''); }

  setReadonly(ro) { this.readonly = ro; this.box.classList.toggle('ro', ro); }

  setError(msg) {
    this.box.classList.toggle('bad', !!msg);
    setText(this.msgEl, msg || '');
    this.msgEl.classList.toggle('hidden', !msg);
  }

  render() {
    const v = this._value;
    const masked = this.kind === 'password' && !this.revealed;
    setText(this.valEl, v ? (masked ? '•'.repeat(v.length) : v) : this.placeholder || (this.optional ? 'optional' : ''));
    this.valEl.classList.toggle('ph', !v);
    if (this.eye) this.eye.replaceChildren(icon(this.revealed ? 'eye-off' : 'eye'));
  }

  async tap() {
    if (this.readonly) return;
    if (this.onTap) return this.onTap(this);
    const res = await openInput({
      label: this.label, kind: this.kind, value: this._value, optional: this.optional,
      presets: typeof this.presets === 'function' ? this.presets() : this.presets || [],
      validate: this.validate, hint: this.hint, reveal: this.revealed,
    });
    if (res === null) return;
    this.value = res;
    this.setError('');
    if (this.onChange) this.onChange(res, this);
  }
}

export class Segmented {
  constructor(options, value, onChange, cls = '') {
    this.options = options;
    this.onChange = onChange;
    this.buttons = new Map();
    this.el = h('div', { class: `seg ${cls}`.trim() });
    for (const o of options) {
      const b = h('button', { type: 'button', onclick: () => this.select(o.value, true) },
        o.icon ? icon(o.icon) : null, o.label);
      this.buttons.set(o.value, b);
      this.el.append(b);
    }
    this.set(value);
  }

  set(value) {
    this.value = value;
    for (const [v, b] of this.buttons) b.classList.toggle('on', v === value);
  }

  select(value, user) {
    if (value === this.value) return;
    this.set(value);
    if (user && this.onChange) this.onChange(value);
  }
}

// Standard sub-page header: [Back] icon Title  ...extra  [refresh]
export function pageBar({ app, iconName, color, title, sub = '', extra = [], onRefresh }) {
  const ic = icon(iconName);
  if (color) ic.style.color = color;
  const titleEl = h('span', { class: 't' }, title);
  const subEl = h('small', null, sub);
  const refresh = onRefresh
    ? h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Refresh', onclick: onRefresh }, icon('refresh'))
    : null;
  const el = h('header', { class: 'bar' },
    h('button', { class: 'btn-back', type: 'button', onclick: () => app.back() }, icon('back'), 'Back'),
    h('div', { class: 'bar-title' }, ic, titleEl, subEl),
    h('div', { class: 'spacer' }),
    extra,
    refresh);
  return { el, titleEl, subEl, refresh };
}

export function spinWhile(btn, promise) {
  if (!btn) return promise;
  btn.classList.add('spin');
  return Promise.resolve(promise).finally(() => btn.classList.remove('spin'));
}
