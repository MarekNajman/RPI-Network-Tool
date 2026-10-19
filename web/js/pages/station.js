// Wi-Fi station: connect to a scanned (or hidden) network, DHCP or static IPv4.

import { api } from '../api.js';
import { h, icon, setText } from '../dom.js';
import { secLabel } from '../status.js';
import { Field, Segmented, confirm, pageBar, runAction } from '../ui.js';
import { V, checkStatic } from '../validate.js';
import { ipv4Fields } from './eth.js';

export function createStation(app) {
  let network = null;
  let hidden = false;
  let security = 'wpa-psk';
  let ipMode = 'auto';

  const bar = pageBar({ app, iconName: 'station', color: 'var(--wifi)', title: 'Connect', sub: '' });

  const fSsid = new Field({ label: 'Network name (SSID)', kind: 'text', validate: V.ssid, placeholder: 'Tap to enter' });
  const secSeg = new Segmented(
    [{ value: 'open', label: 'Open' }, { value: 'wpa-psk', label: 'WPA2' }, { value: 'sae', label: 'WPA3' }],
    'wpa-psk', (v) => { security = v; updateSecurity(); }, 'teal');
  const secInfo = h('div', { class: 'input ro' }, icon('lock'), h('span', { class: 'v' }));
  const secField = h('div', { class: 'field' }, h('label', null, 'Security'), secSeg.el, secInfo);
  const fPass = new Field({ label: 'Password', kind: 'password', validate: V.psk, placeholder: 'Tap to enter' });
  const ipSeg = new Segmented([{ value: 'auto', label: 'DHCP' }, { value: 'manual', label: 'Static IP' }], 'auto',
    (v) => { ipMode = v; updateIp(); }, 'teal');
  const ipField = h('div', { class: 'field' }, h('label', null, 'IPv4 address'), ipSeg.el);
  const f = ipv4Fields();
  const staticGrid = h('div', { class: 'grid2' }, f.address.el, f.mask.el, f.gateway.el, f.dns.el);

  const hint = h('div', { class: 'hint' });
  const connectBtn = h('button', { class: 'btn teal', type: 'button', onclick: connect }, icon('wifi'), 'Connect');
  const form = h('div', { class: 'scroll grow form' },
    h('div', { class: 'grid2' }, fSsid.el, secField),
    h('div', { class: 'grid2' }, fPass.el, ipField),
    staticGrid);
  const card = h('section', { class: 'card fill' }, form, h('div', { class: 'footer' }, hint, connectBtn));
  const el = h('section', { class: 'page', id: 'page-station' }, bar.el, h('main', { class: 'body' }, card));

  function updateSecurity() {
    const open = security === 'open';
    fPass.el.classList.toggle('hidden', open);
    if (!hidden) secInfo.querySelector('.v').textContent = secLabel(security);
  }

  function updateIp() {
    ipSeg.set(ipMode);
    staticGrid.classList.toggle('hidden', ipMode !== 'manual');
    setText(hint, ipMode === 'manual' ? 'Static address for this Wi-Fi network.' : 'The address is assigned by the network (DHCP).');
    hint.classList.remove('bad');
  }

  async function connect() {
    const errors = {};
    const ssid = fSsid.value;
    const e1 = V.ssid(ssid);
    if (e1) errors.ssid = e1;
    if (security !== 'open') {
      const e2 = V.psk(fPass.value);
      if (e2) errors.password = e2;
    }
    let ipv4 = { method: 'auto' };
    if (ipMode === 'manual') {
      const r = checkStatic(f.values());
      Object.assign(errors, r.errors);
      ipv4 = r.cfg;
    }
    showErrors(errors);
    if (Object.keys(errors).length) {
      setText(hint, 'Please fix the highlighted fields.');
      hint.classList.add('bad');
      return;
    }
    if (app.status?.wifi?.mode === 'ap') {
      const ok = await confirm({
        title: 'Stop the access point?', danger: true, ok: 'Connect',
        text: `wlan0 is running an access point. Connecting to “${ssid}” stops it and disconnects all Wi-Fi clients.`,
      });
      if (!ok) return;
    }
    const body = { ssid, security, password: security === 'open' ? '' : fPass.value, hidden, ipv4 };
    const res = await runAction({
      busyText: `Connecting to “${ssid}”…`, busySub: 'This can take up to a minute',
      fn: () => api('wifi/station', body), errorTitle: 'Connection failed', onFieldErrors: showErrors,
    });
    if (res) {
      app.refresh();
      app.back();
    }
  }

  function showErrors(errors) {
    fSsid.setError(errors.ssid);
    fPass.setError(errors.password);
    f.showErrors(errors);
  }

  return {
    el,
    enter(params = {}) {
      network = params.network || null;
      hidden = !!params.hidden || !network;
      security = hidden ? 'wpa-psk' : network.sec_kind;
      ipMode = 'auto';
      setText(bar.titleEl, hidden ? 'Hidden network' : network.ssid);
      setText(bar.subEl, hidden ? '' : `${network.band} · ch ${network.channel}`);
      fSsid.value = hidden ? '' : network.ssid;
      fSsid.setReadonly(!hidden);
      secSeg.set(security);
      secSeg.el.classList.toggle('hidden', !hidden);
      secInfo.classList.toggle('hidden', hidden);
      fPass.value = '';
      fPass.revealed = false;
      fPass.render();
      f.set({});
      // Reconfiguring the current network: start from its current IP settings.
      const w = app.status?.wifi;
      if (network && network.in_use && w && w.method === 'manual') {
        ipMode = 'manual';
        f.set(w);
      }
      showErrors({});
      updateSecurity();
      updateIp();
    },
  };
}
