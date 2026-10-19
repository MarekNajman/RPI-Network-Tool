// Wi-Fi: status strip + Station view (scan & pick network) or Access Point view.

import { api } from '../api.js';
import { h, icon, setText, cidr } from '../dom.js';
import { bandLabel, secLabel, secSupported, wifiPill } from '../status.js';
import { Field, Segmented, alertError, choose, confirm, pageBar, runAction, spinWhile, toast } from '../ui.js';
import { V, prefixFromText, networkOf } from '../validate.js';

const CH = { bg: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13], a: [36, 40, 44, 48] };
const DEFAULT_CH = { bg: 6, a: 36 };

function bars(signal) {
  const n = signal >= 75 ? 4 : signal >= 50 ? 3 : signal >= 30 ? 2 : signal > 0 ? 1 : 0;
  return h('div', { class: 'bars' }, [0, 1, 2, 3].map((i) => h('span', { class: i < n ? 'on' : null })));
}

export function createWifi(app) {
  let wifi = null;
  let ap = null;
  let view = 'station';
  let networks = [];
  let scanning = false;
  let scanError = '';
  let apDirty = false;
  let band = 'bg';

  // ---------------------------------------------------------------- header
  const modeSeg = new Segmented(
    [{ value: 'station', label: 'Station', icon: 'station' }, { value: 'ap', label: 'Access Point', icon: 'ap' }],
    'station', (v) => setView(v), 'teal small');
  modeSeg.el.style.width = '330px';
  const bar = pageBar({ app, iconName: 'wifi', color: 'var(--wifi)', title: 'Wi-Fi', sub: 'wlan0', extra: [modeSeg.el], onRefresh: () => reload(true) });

  // ---------------------------------------------------------------- strip
  const stripIcon = h('span');
  const stripTitle = h('b');
  const stripSub = h('span');
  const stripAction = h('div');
  const strip = h('div', { class: 'strip' }, stripIcon, h('div', { class: 'txt' }, stripTitle, stripSub), stripAction);

  // ---------------------------------------------------------------- station view
  const list = h('div', { class: 'netlist scroll' });
  const scanBtn = h('button', { class: 'btn', type: 'button', onclick: () => scan() }, icon('refresh'), 'Scan');
  const hiddenBtn = h('button', { class: 'btn', type: 'button', onclick: () => app.go('station', { hidden: true }) }, icon('hidden'), 'Hidden network');
  const stationView = h('div', { class: 'row-fill' }, list,
    h('div', { class: 'side' }, scanBtn, hiddenBtn,
      h('div', { class: 'note' }, icon('info'), h('span', null, 'Tap a network to connect. Open, WPA2 and WPA3-Personal networks are supported.'))));

  // ---------------------------------------------------------------- AP view
  const fSsid = new Field({ label: 'Network name (SSID)', kind: 'text', validate: V.ssid, onChange: () => { apDirty = true; } });
  const fPass = new Field({ label: 'Password (WPA2)', kind: 'password', validate: V.psk, reveal: true, onChange: () => { apDirty = true; } });
  const bandSeg = new Segmented([{ value: 'bg', label: '2.4 GHz' }, { value: 'a', label: '5 GHz' }], 'bg', (v) => {
    band = v;
    apDirty = true;
    if (!CH[band].includes(Number(fChan.value))) fChan.value = String(DEFAULT_CH[band]);
  }, 'teal');
  const bandField = h('div', { class: 'field' }, h('label', null, 'Band'), bandSeg.el);
  const fChan = new Field({
    label: 'Channel', kind: 'choice',
    onTap: async (fld) => {
      const v = await choose({
        title: `Channel (${bandLabel(band)})`, value: Number(fld.value), cols: band === 'bg' ? 5 : 4,
        options: CH[band].map((c) => ({ value: c, label: String(c) })),
      });
      if (v != null) { fld.value = String(v); apDirty = true; }
    },
  });
  const fAddr = new Field({ label: 'AP address / prefix', kind: 'cidr', placeholder: '10.42.0.1/24',
    presets: ['10.42.0.1/24', '192.168.50.1/24', '172.20.0.1/24'], onChange: () => { apDirty = true; } });
  const natNote = h('div', { class: 'note' }, icon('info'),
    h('span', null, 'Clients get an address via DHCP and reach the network through eth0 (NAT).'));
  const apHint = h('div', { class: 'hint' });
  const stopApBtn = h('button', { class: 'btn danger', type: 'button', onclick: stopAp }, icon('power'), 'Stop AP');
  const startApBtn = h('button', { class: 'btn teal', type: 'button', onclick: startAp }, icon('ap'), 'Start access point');
  const apView = h('section', { class: 'card fill' },
    h('div', { class: 'scroll grow form' },
      h('div', { class: 'grid2' }, fSsid.el, fPass.el),
      h('div', { class: 'grid3' }, bandField, fChan.el, fAddr.el),
      natNote),
    h('div', { class: 'footer' }, stopApBtn, apHint, startApBtn));

  const main = h('main', { class: 'body vert' }, strip, stationView, apView);
  const el = h('section', { class: 'page', id: 'page-wifi' }, bar.el, main);

  // ---------------------------------------------------------------- rendering
  function renderStrip() {
    const w = wifi;
    setText(bar.subEl, w?.iface || 'wlan0');
    stripAction.replaceChildren();
    let ic = 'wifi';
    let title = '';
    let sub = '';
    if (!w) {
      title = 'Loading…';
    } else if (!w.present) {
      ic = 'warn'; title = 'No Wi-Fi adapter found'; sub = `Interface ${w.iface} does not exist.`;
    } else if (!w.radio_hw) {
      ic = 'warn'; title = 'Wi-Fi is blocked (rfkill)';
      sub = 'Set the Wi-Fi country: sudo raspi-config → Localisation → WLAN Country.';
    } else if (!w.radio) {
      ic = 'radio'; title = 'Wi-Fi radio is off'; sub = 'Turn it on to scan or start an access point.';
      stripAction.append(h('button', { class: 'btn teal', type: 'button', onclick: radioOn }, icon('power'), 'Turn on'));
    } else if (w.mode === 'ap') {
      ic = 'ap'; title = `Access point “${w.ssid}”`;
      sub = `${cidr(w.address, w.prefix)} · ${bandLabel(w.band)} ch ${w.channel ?? '?'} · ${wifiPill(w)[1].replace('AP active · ', '')}`;
    } else if (w.mode === 'station' && w.state === 'connected') {
      ic = 'station'; title = `Connected to “${w.ssid}”`;
      sub = `${cidr(w.address, w.prefix)} · ${w.method === 'manual' ? 'static' : 'DHCP'}${w.signal != null ? ` · signal ${w.signal} %` : ''}`;
    } else if (w.state === 'connecting') {
      ic = 'station'; title = 'Connecting…'; sub = w.ssid || '';
    } else {
      ic = 'station'; title = 'Not connected'; sub = 'Pick a network below or switch to Access Point.';
    }
    if (w && w.present && w.radio && (!w.regdomain || w.regdomain === '00')) {
      sub = `Wi-Fi country not set (raspi-config) · ${sub}`;
    }
    stripIcon.replaceChildren(icon(ic, 'lead'));
    setText(stripTitle, title);
    setText(stripSub, sub);
  }

  function renderList() {
    list.replaceChildren();
    const w = wifi;
    if (w && w.mode === 'ap') {
      list.append(h('div', { class: 'empty' }, icon('ap'), h('b', null, 'Access point is active'),
        'Scanning needs the radio in station mode.',
        h('button', { class: 'btn danger', type: 'button', onclick: stopAp }, icon('power'), 'Stop access point')));
      return;
    }
    if (w && (!w.present || !w.radio || !w.radio_hw)) {
      list.append(h('div', { class: 'empty' }, icon('radio'), h('b', null, 'Wi-Fi is not available'), 'See the status above.'));
      return;
    }
    if (!networks.length) {
      if (scanning) list.append(h('div', { class: 'empty' }, h('div', { class: 'spinner' }), 'Scanning for networks…'));
      else if (scanError) list.append(h('div', { class: 'empty' }, icon('warn'), h('b', null, 'Scan failed'), scanError));
      else list.append(h('div', { class: 'empty' }, icon('wifi'), h('b', null, 'No networks found'), 'Tap Scan to search again.'));
      return;
    }
    for (const n of networks) {
      const supported = secSupported(n.sec_kind);
      const meta = `${secLabel(n.sec_kind)}${supported ? '' : ' (not supported)'} · ${n.band} · ch ${n.channel}`;
      const row = h('button', {
        class: `net${n.in_use ? ' current' : ''}${supported ? '' : ' unsupported'}`, type: 'button',
        onclick: () => pick(n),
      },
      bars(n.signal),
      h('div', { class: 'mid' }, h('div', { class: 'ssid' }, n.ssid), h('div', { class: 'meta' }, meta)),
      n.in_use ? h('span', { class: 'tag' }, 'Connected') : null,
      icon(n.sec_kind === 'open' ? 'unlock' : 'lock', 'sec'),
      icon('chevron', 'go'));
      list.append(row);
    }
  }

  function renderAp() {
    const active = wifi?.mode === 'ap';
    stopApBtn.classList.toggle('hidden', !active);
    startApBtn.replaceChildren(icon('ap'), active ? 'Apply & restart AP' : 'Start access point');
    setText(apHint, active ? 'Changes restart the AP - clients reconnect.' : (ap?.exists ? 'Saved AP settings loaded.' : 'Suggested settings - change them as needed.'));
  }

  function fillAp() {
    if (!ap) return;
    fSsid.value = ap.ssid;
    fPass.value = ap.password;
    band = ap.band === 'a' ? 'a' : 'bg';
    bandSeg.set(band);
    fChan.value = String(ap.channel || DEFAULT_CH[band]);
    fAddr.value = `${ap.address}/${ap.prefix}`;
    for (const x of [fSsid, fPass, fChan, fAddr]) x.setError('');
    apDirty = false;
  }

  function setView(v) {
    view = v;
    modeSeg.set(v);
    stationView.classList.toggle('hidden', v !== 'station');
    apView.classList.toggle('hidden', v !== 'ap');
    if (v === 'station') {
      renderList();
      if (!networks.length && !scanning && wifi && wifi.mode !== 'ap' && wifi.radio) scan();
    } else {
      renderAp();
    }
  }

  // ---------------------------------------------------------------- actions
  async function reload(force = false) {
    try {
      const r = await spinWhile(bar.refresh, api('wifi'));
      wifi = r.wifi;
      ap = r.ap;
      if (force || !apDirty) fillAp();
      renderStrip();
      renderAp();
      if (view === 'station') {
        renderList();
        if (force && wifi.mode !== 'ap' && wifi.radio) scan();
      }
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function scan() {
    if (scanning) return;
    scanning = true;
    scanError = '';
    if (!networks.length) renderList();
    scanBtn.classList.add('disabled');
    try {
      const r = await api('wifi/scan', {});
      networks = r.networks || [];
    } catch (e) {
      scanError = e.message;
      networks = [];
    } finally {
      scanning = false;
      scanBtn.classList.remove('disabled');
      if (view === 'station') renderList();
    }
  }

  function pick(n) {
    if (!secSupported(n.sec_kind)) {
      alertError('Network not supported', `“${n.ssid}” uses ${secLabel(n.sec_kind)}. NetPanel supports open, WPA2-Personal and WPA3-Personal networks.`);
      return;
    }
    app.go('station', { network: n });
  }

  async function radioOn() {
    const res = await runAction({ busyText: 'Turning Wi-Fi on…', fn: () => api('wifi/radio', { on: true }) });
    if (res) { await reload(true); app.refresh(); }
  }

  async function stopAp() {
    const ok = await confirm({
      title: 'Stop access point?', danger: true, ok: 'Stop AP',
      text: 'All Wi-Fi clients will be disconnected. wlan0 returns to station mode and may reconnect to a known network.',
    });
    if (!ok) return;
    const res = await runAction({ busyText: 'Stopping access point…', fn: () => api('wifi/ap/stop', {}) });
    if (res) {
      networks = [];
      await reload(false);
      setView('station');
      app.refresh();
    }
  }

  async function startAp() {
    const errors = {};
    const e1 = V.ssid(fSsid.value); if (e1) errors.ssid = e1;
    const e2 = V.psk(fPass.value); if (e2) errors.password = e2;
    const e3 = V.cidr(fAddr.value); if (e3) errors.address = e3;
    const [addr, pfx] = fAddr.value.split('/');
    const prefix = prefixFromText(pfx);
    if (!e3 && (prefix < 16 || prefix > 30)) errors.address = 'AP prefix must be between /16 and /30';
    const eth = app.status?.eth;
    if (!errors.address && eth?.address && eth.prefix != null) {
      const p = Math.min(prefix, eth.prefix);
      if (networkOf(addr, p) === networkOf(eth.address, p)) errors.address = `Overlaps the Ethernet subnet ${networkOf(eth.address, eth.prefix)}/${eth.prefix}`;
    }
    showApErrors(errors);
    if (Object.keys(errors).length) return;

    const channel = Number(fChan.value);
    const w = wifi;
    const leaving = w?.mode === 'station' && w.ssid ? `wlan0 leaves “${w.ssid}” and becomes an access point. ` : '';
    const ok = await confirm({
      title: w?.mode === 'ap' ? 'Restart access point?' : 'Start access point?',
      text: `${leaving}Clients get addresses from ${networkOf(addr, prefix)}/${prefix} and internet via eth0 (NAT).`,
      code: `SSID      ${fSsid.value}\nPassword  ${fPass.value}\nBand      ${bandLabel(band)} · channel ${channel}\nAddress   ${addr}/${prefix}`,
      ok: 'Start AP', okClass: 'teal', iconName: 'ap',
    });
    if (!ok) return;
    const body = { ssid: fSsid.value, password: fPass.value, band, channel, address: addr, prefix };
    const res = await runAction({
      busyText: 'Starting access point…', busySub: 'This can take up to 40 seconds',
      fn: () => api('wifi/ap', body), errorTitle: 'Access point failed', onFieldErrors: showApErrors,
    });
    if (res) {
      apDirty = false;
      await reload(true);
      app.refresh();
    }
  }

  function showApErrors(errors) {
    fSsid.setError(errors.ssid);
    fPass.setError(errors.password);
    fChan.setError(errors.channel || errors.band);
    fAddr.setError(errors.address || errors.prefix);
  }

  return {
    el,
    enter() {
      wifi = app.status?.wifi || wifi;
      networks = [];
      scanError = '';
      renderStrip();
      setView(wifi?.mode === 'ap' ? 'ap' : 'station');
      reload(false).then(() => {
        if (view === 'station' && !networks.length && !scanning && wifi && wifi.mode !== 'ap' && wifi.radio) scan();
      });
    },
    update(s) {
      if (!s.wifi) return;
      const modeChanged = wifi && wifi.mode !== s.wifi.mode;
      wifi = s.wifi;
      renderStrip();
      if (modeChanged) {
        renderAp();
        if (view === 'station') renderList();
      }
    },
  };
}

