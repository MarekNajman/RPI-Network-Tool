// Tiny DOM helpers - no framework needed for a handful of screens.

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export const icon = (name, cls = '') => h('span', { class: `i i-${name} ${cls}`.trim(), 'aria-hidden': 'true' });

export function setText(el, text) {
  const t = text == null ? '' : String(text);
  if (el.textContent !== t) el.textContent = t;
}

export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function show(el, visible) {
  el.classList.toggle('hidden', !visible);
}

export const cidr = (addr, prefix) => (addr ? (prefix != null ? `${addr}/${prefix}` : addr) : '—');

export function speedText(mbps) {
  if (!mbps) return '';
  return mbps >= 1000 ? `${mbps / 1000} Gb/s` : `${mbps} Mb/s`;
}

// Key/value list: rows = [[label, value, ddClass?], ...]
export function kvList(rows, stacked = false) {
  const dl = h('dl', { class: stacked ? 'kv stack' : 'kv' });
  for (const [k, v, cls] of rows) dl.append(h('dt', null, k), h('dd', { class: cls || null }, v ?? '—'));
  return dl;
}

export function clearToasts() {
  document.querySelectorAll('.toast').forEach((t) => t.remove());
}

export function pill(kind, text) {
  return h('span', { class: `pill ${kind}` }, text);
}

export function setPill(el, kind, text) {
  el.className = `pill ${kind}`;
  setText(el, text);
}
