// Change the IP settings of a Milesight camera / NVR (Smart Tools "modify"
// command, addressed by MAC - works even when the camera is in another subnet).
// The camera checks its admin user name + password.

import { api } from '../api.js';
import { h, icon, setText } from '../dom.js';
import { Field, confirm, pageBar, runAction } from '../ui.js';
import { checkStatic, maskFromPrefix, parseIPv4, prefixFromText, sameNet } from '../validate.js';
import { MASK_PRESETS, DNS_PRESETS, gatewayPresets } from './eth.js';
import { camTitle } from './cams.js';

const PORT_PRESETS = ['80', '8080', '8000'];

function checkPort(v) {
  const s = String(v || '').trim();
  if (!/^\d{1,5}$/.test(s)) return 'Port must be 1-65535';
  const n = Number(s);
  return n >= 1 && n <= 65535 ? '' : 'Port must be 1-65535';
}

export function createCamIp(app) {
  let cam = null;
  let iface = '';
  let network = null;   // the interface network {iface, address, network, prefix, cidr}

  const bar = pageBar({ app, iconName: 'camera', color: 'var(--cam)', title: 'Change camera IP', sub: '' });

  // ---- summary strip: what the camera has now
  const sumTitle = h('b');
  const sumText = h('span');
  const sumBadge = h('span', { class: 'badge warn hidden' });
  const strip = h('div', { class: 'strip cam-strip' }, icon('camera', 'lead'),
    h('div', { class: 'txt' }, h('b', null, sumTitle, sumBadge), sumText));

  // ---- form
  const f = {};
  f.address = new Field({ label: 'New IP address', kind: 'ip', placeholder: '192.168.1.64',
    presets: () => (cam && cam.in_subnet ? [cam.ip] : []) });
  f.mask = new Field({ label: 'Subnet mask', kind: 'mask', placeholder: '255.255.255.0', presets: MASK_PRESETS });
  f.gateway = new Field({ label: 'Gateway', kind: 'ip', placeholder: '192.168.1.1',
    presets: () => [...new Set([...gatewayPresets(f.address, f.mask), ...(network?.gateway ? [network.gateway] : [])])] });
  f.dns = new Field({ label: 'DNS server', kind: 'ip', optional: true, presets: DNS_PRESETS,
    hint: 'Empty = keep the camera\'s DNS server' });
  f.port = new Field({ label: 'HTTP port', kind: 'number', placeholder: '80', presets: PORT_PRESETS, validate: checkPort });
  f.user = new Field({ label: 'Camera user', kind: 'text', placeholder: 'admin' });
  f.pass = new Field({ label: 'Camera password', kind: 'password', placeholder: 'required' });
  const all = Object.values(f);
  const hint = h('div', { class: 'hint' });
  const applyBtn = h('button', { class: 'btn pink', type: 'button', onclick: apply }, icon('check'), 'Apply');
  const form = h('section', { class: 'card fill' },
    h('h3', null, 'New network settings'),
    h('div', { class: 'grid3' }, f.address.el, f.mask.el, f.gateway.el, f.dns.el, f.port.el, f.user.el, f.pass.el,
      h('div', { class: 'footer span2' }, hint, applyBtn)));
  for (const x of all) x.onChange = () => { x.setError(''); setHint(); };

  const el = h('section', { class: 'page', id: 'page-camip' }, bar.el, h('main', { class: 'body vert' }, strip, form));

  function setHint(text, bad = false) {
    if (text === undefined) {
      const p = prefixFromText(f.mask.value);
      const ok = parseIPv4(f.address.value) && p != null && network;
      if (ok && !sameNet(f.address.value, network.address, network.prefix)) {
        text = `Note: ${f.address.value} is outside ${network.iface} (${network.cidr}) - NetPanel will not reach the camera afterwards.`;
        bad = true;
      } else {
        text = 'Tap a field to edit it. The camera checks the user name and password.';
      }
    }
    setText(hint, text);
    hint.classList.toggle('bad', bad);
  }

  function fill() {
    setText(bar.subEl, `${camTitle(cam)} · ${(cam.mac || '').toUpperCase()}`);
    setText(sumTitle, `${camTitle(cam)}${cam.name && cam.model ? ` · ${cam.name}` : ''}`);
    setText(sumText, `now ${cam.ip}${cam.netmask ? ' / ' + cam.netmask : ''} · gateway ${cam.gateway || '—'} · ${iface}`);
    const badge = !cam.in_subnet ? 'other subnet' : cam.activated === false ? 'not activated' : '';
    setText(sumBadge, badge);
    sumBadge.classList.toggle('hidden', !badge);

    if (cam.in_subnet) {
      f.address.value = cam.ip;
      f.mask.value = cam.netmask || (network ? maskFromPrefix(network.prefix) : '255.255.255.0');
      f.gateway.value = cam.gateway || network?.gateway || '';
    } else {
      // Camera lives elsewhere (e.g. factory default 192.168.5.190): suggest our network.
      f.address.placeholder = network && network.prefix === 24 ? network.network.replace(/\.0$/, '.x') : network ? `in ${network.cidr}` : '192.168.1.64';
      f.address.value = '';
      f.mask.value = network ? maskFromPrefix(network.prefix) : cam.netmask || '255.255.255.0';
      f.gateway.value = network?.gateway || '';
    }
    f.dns.placeholder = cam.dns ? `keep ${cam.dns}` : 'optional';
    f.dns.value = '';
    f.port.value = String(cam.http_port || 80);
    f.user.value = 'admin';
    f.pass.value = '';
    for (const x of all) x.setError('');
    setHint();
  }

  function showErrors(errors) {
    f.address.setError(errors.address);
    f.mask.setError(errors.mask || errors.prefix);
    f.gateway.setError(errors.gateway);
    f.dns.setError(errors.dns);
    f.port.setError(errors.http_port);
    f.user.setError(errors.username);
    f.pass.setError(errors.password);
  }

  function validate() {
    const { errors } = checkStatic({ address: f.address.value, mask: f.mask.value, gateway: f.gateway.value, dns: f.dns.value });
    if (!f.gateway.value.trim()) errors.gateway = 'Gateway is required';
    const ep = checkPort(f.port.value);
    if (ep) errors.http_port = ep;
    if (!f.user.value.trim()) errors.username = 'User name is required';
    if (!f.pass.value) errors.password = 'Password is required';
    return errors;
  }

  async function apply() {
    const errors = validate();
    showErrors(errors);
    if (Object.keys(errors).length) {
      setHint('Please fix the highlighted fields.', true);
      return;
    }
    const mask = maskFromPrefix(prefixFromText(f.mask.value));
    const body = {
      mac: cam.mac, iface, address: f.address.value.trim(), mask, gateway: f.gateway.value.trim(),
      dns: f.dns.value.trim(), http_port: Number(f.port.value), username: f.user.value.trim(), password: f.pass.value,
    };
    const outside = network && !sameNet(body.address, network.address, network.prefix);
    const ok = await confirm({
      title: 'Change camera IP?',
      text: `${camTitle(cam)} (${(cam.mac || '').toUpperCase()}) gets the new settings and reconnects. `
        + 'Recordings or live views using the old address stop working.',
      code: `Address  ${cam.ip} → ${body.address}/${prefixFromText(mask)}\nGateway  ${body.gateway}\n`
        + `DNS      ${body.dns || (cam.dns ? cam.dns + ' (unchanged)' : '—')}\nHTTP     ${body.http_port}`,
      extra: outside ? h('div', { class: 'note warn' }, icon('warn'),
        h('span', null, `${body.address} is outside ${network.iface} (${network.cidr}). NetPanel will not be able to reach the camera after the change.`)) : null,
      ok: 'Change IP', okClass: 'pink', iconName: 'camera',
    });
    if (!ok) return;
    const res = await runAction({
      busyText: 'Sending new settings to the camera…',
      busySub: 'Waiting for the camera to confirm (up to 10 s)',
      fn: () => api('cams/ip', body),
      errorTitle: 'IP change failed',
      onFieldErrors: showErrors,
    });
    f.pass.value = '';  // never keep the camera password around
    if (res) {
      app.back();
      app.pages.cams?.rescanSoon(iface);
    }
  }

  window.addEventListener('netpanel-reset', () => { f.pass.value = ''; });

  return {
    el,
    enter(params = {}) {
      if (params.cam) {
        cam = params.cam;
        iface = params.iface || cam.iface || '';
        network = params.network || null;
        if (network) network = { ...network, gateway: app.status?.[iface.startsWith('w') ? 'wifi' : 'eth']?.gateway || '' };
        fill();
      }
    },
  };
}
