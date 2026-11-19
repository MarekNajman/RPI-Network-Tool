// Cameras: find Milesight cameras / NVRs (Smart Tools protocol, also in other
// subnets) and ONVIF cameras of any brand on the chosen interface. The server
// does the work in the background; this page starts the search and polls.
// Tapping a row shows the details; Milesight devices offer "Change IP".

import { api } from '../api.js';
import { h, icon, setText, kvList } from '../dom.js';
import { session } from '../session.js';
import { Segmented, confirm, info, pageBar, toast } from '../ui.js';

const POLL_MS = 500;
const RESCAN_AFTER_CHANGE_MS = 3500; // the camera needs a moment to apply a new address
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hhmm = (t) => {
  const d = new Date(t * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};
const SOURCE = { milesight: 'Milesight', onvif: 'ONVIF' };

export function camTitle(c) {
  return c.model || c.name || (c.vendor ? `${c.vendor} device` : 'Unknown device');
}

function webText(c) {
  const parts = [];
  if (c.http_port && c.http_enabled !== false) parts.push(`HTTP ${c.http_port}`);
  if (c.https_port && c.https_enabled) parts.push(`HTTPS ${c.https_port}`);
  return parts.join(' · ');
}

export function createCams(app) {
  let cams = null;      // last snapshot from the server
  let networks = [];    // [{iface, address, network, prefix, cidr}]
  let iface = '';
  let polling = false;
  let starting = false;
  let rescanTimer = null;
  const rows = new Map(); // ip -> {el, cells, cam}

  const bar = pageBar({ app, iconName: 'camera', color: 'var(--cam)', title: 'Cameras', sub: 'Milesight · ONVIF' });

  // ---- toolbar: [eth0|wlan0]  status  [Search|Stop]
  const segBox = h('div', { class: 'cam-ifaces' });
  let seg = null;
  const line1 = h('div', { class: 'l1' });
  const line2 = h('div', { class: 'l2' });
  const progress = h('div', { class: 'progress indet hidden' }, h('span'));
  const statusBox = h('div', { class: 'scan-status' }, line1, progress, line2);
  const goBtn = h('button', { class: 'btn cam-go', type: 'button', onclick: startOrStop });
  const toolbar = h('section', { class: 'card scan-bar cam-bar' }, segBox, statusBox, goBtn);

  // ---- results
  const head = h('div', { class: 'cam-row head' },
    h('span', null, 'Device'), h('span', null, 'IP address'), h('span', null, 'MAC / vendor'), h('span', null, 'Found via'));
  const list = h('div', { class: 'cam-list scroll' });
  const emptyIcon = h('span', { class: 'eic' });
  const emptyTitle = h('b');
  const emptyText = h('span');
  const empty = h('div', { class: 'empty' }, emptyIcon, emptyTitle, emptyText);
  const results = h('section', { class: 'card scan-results cam-results' }, head, list, empty);

  const el = h('section', { class: 'page', id: 'page-cams' }, bar.el, h('main', { class: 'body vert' }, toolbar, results));

  // ---------------------------------------------------------------- helpers
  const running = () => cams?.state === 'running';
  const isActive = () => el.classList.contains('active') && session.loggedIn;
  const net = () => networks.find((n) => n.iface === iface);

  function buildSeg() {
    const opts = networks.map((n) => ({ value: n.iface, label: n.iface, icon: n.iface.startsWith('w') ? 'wifi' : 'ethernet' }));
    if (!opts.length) {
      seg = null;
      segBox.replaceChildren(h('span', { class: 'dim' }, 'No network'));
      return;
    }
    seg = new Segmented(opts, iface, (v) => {
      if (running()) { seg.set(iface); return; }
      iface = v;
      render();
    }, 'pink');
    segBox.replaceChildren(seg.el);
  }

  // ---------------------------------------------------------------- table
  function makeRow(cam) {
    const c = {
      model: h('b'), name: h('small'),
      ip: h('b', { class: 'num' }), ipSub: h('small'),
      mac: h('b', { class: 'num' }), ven: h('small'),
      via: h('span', { class: 'via' }),
    };
    const r = { cells: c, cam };
    r.el = h('button', { class: 'cam-row', type: 'button', onclick: () => detail(r.cam) },
      h('span', { class: 'two' }, c.model, c.name),
      h('span', { class: 'two' }, c.ip, c.ipSub),
      h('span', { class: 'two' }, c.mac, c.ven),
      c.via);
    return r;
  }

  function fillRow(r, cam) {
    r.cam = cam;
    const c = r.cells;
    setText(c.model, camTitle(cam));
    setText(c.name, cam.name && cam.model ? cam.name : cam.vendor || '—');
    setText(c.ip, cam.ip);
    let sub = webText(cam) || '—';
    let warn = false;
    if (!cam.in_subnet) { sub = 'other subnet'; warn = true; } else if (cam.activated === false) { sub = 'not activated'; warn = true; }
    setText(c.ipSub, sub);
    c.ipSub.classList.toggle('warn', warn);
    setText(c.mac, (cam.mac || '').toUpperCase() || '—');
    setText(c.ven, cam.vendor || 'unknown vendor');
    c.via.replaceChildren(
      ...(cam.kind === 'IPC' || cam.kind === 'NVR' ? [h('span', { class: 'badge kind' }, cam.kind)] : []),
      ...(cam.sources || []).map((s) => h('span', { class: `badge src-${s}` }, SOURCE[s] || s)));
    r.el.classList.toggle('warn', warn);
  }

  function renderCams(list_) {
    const seen = new Set();
    let prev = null;
    for (const cam of list_) {
      seen.add(cam.ip);
      let r = rows.get(cam.ip);
      if (!r) {
        r = makeRow(cam);
        rows.set(cam.ip, r);
      }
      fillRow(r, cam);
      const want = prev ? prev.el.nextSibling : list.firstChild;
      if (want !== r.el) list.insertBefore(r.el, want);
      prev = r;
    }
    for (const [ip, r] of rows) {
      if (!seen.has(ip)) {
        r.el.remove();
        rows.delete(ip);
      }
    }
  }

  function clearCams() {
    rows.clear();
    list.replaceChildren();
  }

  // ---------------------------------------------------------------- detail
  async function detail(cam) {
    const n = networks.find((x) => x.iface === cam.iface) || net();
    const kv = [];
    if (cam.name && cam.model) kv.push(['Name', cam.name, 'wrap']);
    if (cam.oem_model && cam.oem_model !== cam.model) kv.push(['OEM model', cam.oem_model]);
    const kind = cam.kind === 'NVR' ? 'NVR (recorder)' : cam.kind === 'IPC' ? 'IP camera' : cam.kind || 'device';
    kv.push(['Type · vendor', `${kind} · ${cam.vendor || 'unknown vendor'}`]);
    kv.push(['IP address', cam.netmask ? `${cam.ip} / ${cam.netmask}` : cam.ip]);
    if (cam.gateway || cam.dns) kv.push(['Gateway · DNS', `${cam.gateway || '—'} · ${cam.dns || '—'}`]);
    if (webText(cam)) kv.push(['Web', webText(cam)]);
    kv.push(['MAC address', (cam.mac || '').toUpperCase() || '—']);
    const fw = [cam.firmware, cam.hardware && `HW ${cam.hardware}`, cam.serial && `SN ${cam.serial}`].filter(Boolean);
    if (fw.length) kv.push(['Firmware', fw.join(' · '), 'wrap']);
    if (cam.activated != null) kv.push(['Status', cam.activated ? 'activated' : 'NOT activated']);
    kv.push(['Found via', (cam.sources || []).map((s) => SOURCE[s] || s).join(' + ')]);
    if (cam.onvif_url) kv.push(['ONVIF', cam.onvif_url, 'wrap']);

    // Problems first - the dialog scrolls, warnings must not hide below the fold.
    const notes = [];
    if (!cam.in_subnet) {
      notes.push(h('div', { class: 'note warn' }, icon('warn'), h('span', null,
        `Outside the network of ${cam.iface || iface}${n ? ` (${n.cidr})` : ''} - the camera cannot be reached from here.`
        + (cam.can_modify ? ' Change its IP to an address in this network.' : ''))));
    }
    if (cam.activated === false) {
      notes.push(h('div', { class: 'note warn' }, icon('info'), h('span', null,
        'Not activated: no admin password is set yet. Activate the camera in its web interface (or with Smart Tools) '
        + 'first - until then it usually refuses an IP change.')));
    }
    const extra = [...notes, kvList(kv)];
    if (!cam.can_modify) {
      if (cam.sources?.includes('onvif') && !cam.sources.includes('milesight')) {
        extra.push(h('div', { class: 'note' }, icon('info'), h('span', null,
          'Found via ONVIF - the IP of this device can be changed in its own web interface.')));
      }
      return info({ title: camTitle(cam), iconName: 'camera', extra });
    }
    const change = await confirm({
      title: camTitle(cam), iconName: 'camera', extra, ok: 'Change IP', cancel: 'Close', okClass: 'pink',
    });
    if (change) app.go('camip', { cam, iface: cam.iface || iface, network: n || null });
  }

  // ---------------------------------------------------------------- status / empty state
  function render() {
    const s = cams;
    const busy = running();
    const n = net();
    goBtn.replaceChildren(icon(busy ? 'stop' : 'camera'), busy ? 'Stop' : 'Search');
    goBtn.classList.toggle('stop', busy);
    goBtn.disabled = starting || (!busy && !n);
    progress.classList.toggle('hidden', !busy);
    line1.classList.toggle('bad', !busy && s?.state === 'error');

    if (busy) {
      setText(line1, `Searching… ${s.count} found`);
      setText(line2, `${s.iface} · ${s.network}`);
    } else if (s && s.state === 'error') {
      setText(line1, 'Search failed');
      setText(line2, s.message || '');
    } else if (s && (s.state === 'done' || s.state === 'stopped') && s.iface === iface) {
      setText(line1, `${s.count} device${s.count === 1 ? '' : 's'}${s.state === 'stopped' ? ' · stopped' : ''}`);
      setText(line2, `${s.iface} · ${hhmm(s.finished)} · ${s.duration} s`);
    } else if (n) {
      setText(line1, n.cidr);
      setText(line2, `${n.iface} · tap Search to start`);
    } else {
      setText(line1, 'No network');
      setText(line2, 'connect Ethernet or Wi-Fi first');
    }

    const shown = s?.iface === iface || busy ? s?.cams || [] : [];
    const showList = shown.length > 0;
    head.classList.toggle('hidden', !showList);
    list.classList.toggle('hidden', !showList);
    empty.classList.toggle('hidden', showList);
    if (showList) renderCams(shown);
    else {
      clearCams();
      renderEmpty(s);
    }
  }

  function renderEmpty(s) {
    let ic = 'camera';
    let title = 'No search yet';
    let text = 'Finds Milesight cameras and NVRs - also with an address from another subnet - and ONVIF cameras of any brand.';
    if (running()) {
      title = 'Searching…';
      text = 'Cameras appear here as they answer.';
    } else if (s && s.state === 'error') {
      ic = 'warn';
      title = 'Search failed';
      text = s.message || '';
    } else if (s && (s.state === 'done' || s.state === 'stopped') && s.iface === iface) {
      title = 'No cameras found';
      text = 'Nothing answered on this interface. Check the cable / PoE switch; ONVIF must be enabled on non-Milesight cameras.';
    }
    emptyIcon.replaceChildren(icon(ic));
    setText(emptyTitle, title);
    setText(emptyText, text);
  }

  // ---------------------------------------------------------------- actions
  async function startOrStop() {
    if (running()) return stop();
    if (!net()) return;
    clearTimeout(rescanTimer);
    starting = true;
    render();
    try {
      const r = await api('cams/start', { iface });
      clearCams();
      cams = r.cams;
    } catch (e) {
      if (!e.authRequired) toast(e.message, 'err');
    } finally {
      starting = false;
      render();
    }
    poll();
  }

  async function stop() {
    starting = true;
    render();
    try {
      cams = (await api('cams/stop', {})).cams;
    } catch (e) {
      if (!e.authRequired) toast(e.message, 'err');
    } finally {
      starting = false;
      render();
    }
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      while (running() && isActive()) {
        await sleep(POLL_MS);
        if (!isActive()) break;
        try {
          cams = (await api('cams/status')).cams;
          render();
        } catch (e) {
          if (e.authRequired) break;
        }
      }
    } finally {
      polling = false;
    }
  }

  async function load() {
    try {
      const r = await api('cams');
      cams = r.cams;
      networks = r.networks || [];
      const names = networks.map((n) => n.iface);
      if (!names.includes(iface)) iface = cams?.iface && names.includes(cams.iface) ? cams.iface : r.suggested || names[0] || '';
      buildSeg();
      if (r.error) toast(r.error, 'err');
      render();
      poll();
    } catch (e) {
      if (!e.authRequired) toast(e.message, 'err');
    }
  }

  window.addEventListener('netpanel-reset', () => clearTimeout(rescanTimer));

  return {
    el,
    keepOnBack: false,
    enter() {
      render();
      load();
    },
    // After an IP change: search again once the camera had time to apply it.
    rescanSoon(ifaceName) {
      clearTimeout(rescanTimer);
      if (ifaceName) iface = ifaceName;
      rescanTimer = setTimeout(() => {
        if (isActive() && !running()) startOrStop();
      }, RESCAN_AFTER_CHANGE_MS);
    },
  };
}
