// LAN scan: find the devices in a directly connected network and list
// Name / IP / MAC / Vendor. The server does the work (ARP discovery, vendor
// lookup, name resolution) in the background; this page starts it and polls.

import { api } from '../api.js';
import { h, icon, setText, kvList } from '../dom.js';
import { session } from '../session.js';
import { Field, info, pageBar, toast } from '../ui.js';
import { V, parseIPv4, prefixFromText, networkOf } from '../validate.js';

const POLL_MS = 600;
const MIN_PREFIX = 22; // max 1024 addresses - same limit as the server
const PREFIX_PRESETS = ['24', '23', '22', '25', '26', '27', '28', '29', '30', '32']
  .map((v) => ({ value: v, label: `/${v}` }));
const SOURCES = [['dhcp', 'DHCP lease'], ['mdns', 'mDNS'], ['netbios', 'NetBIOS'], ['dns', 'Reverse DNS']];

const sourceLabel = (src) => (src === 'this device' ? 'hostname' : SOURCES.find(([k]) => k === src)?.[1] || src || '?');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hhmm = (t) => {
  const d = new Date(t * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

function checkPrefix(v) {
  const p = prefixFromText(v);
  if (p == null || p < 1) return 'Prefix must be 1-32';
  if (p < MIN_PREFIX) return `Too large: /${p} has ${(2 ** (32 - p)).toLocaleString('en')} addresses. Max /${MIN_PREFIX} (1024).`;
  return '';
}

export function createScan(app) {
  let scan = null;      // last snapshot from the server
  let networks = [];    // directly connected networks [{iface, network, prefix, cidr}]
  let suggested = null;
  let dirty = false;
  let polling = false;
  let starting = false;
  let formError = '';
  const rows = new Map(); // ip -> {el, cells, host}

  const bar = pageBar({ app, iconName: 'radar', color: 'var(--scan)', title: 'LAN scan', sub: 'Devices on the local network' });

  // ---- toolbar: [network] / [prefix]  status  [Scan|Stop]
  const fNet = new Field({ label: 'Network address', kind: 'ip', placeholder: '192.168.1.0', compact: true, cls: 'f-net', onChange: changed });
  const fPfx = new Field({ label: 'Prefix (max /22)', kind: 'prefix', placeholder: '24', compact: true, cls: 'f-pfx',
    presets: PREFIX_PRESETS, validate: checkPrefix, onChange: changed });
  const line1 = h('div', { class: 'l1' });
  const line2 = h('div', { class: 'l2' });
  const barFill = h('span');
  const progress = h('div', { class: 'progress hidden' }, barFill);
  const statusBox = h('div', { class: 'scan-status' }, line1, progress, line2);
  const goBtn = h('button', { class: 'btn scan-go', type: 'button', onclick: startOrStop });
  const toolbar = h('section', { class: 'card scan-bar' },
    fNet.el, h('span', { class: 'slash' }, '/'), fPfx.el, statusBox, goBtn);

  // ---- results
  const head = h('div', { class: 'scan-row head' },
    h('span', null, 'Name'), h('span', null, 'IP'), h('span', null, 'MAC'), h('span', null, 'Vendor'));
  const list = h('div', { class: 'scan-list scroll' });
  const emptyIcon = h('span', { class: 'eic' });
  const emptyTitle = h('b');
  const emptyText = h('span');
  const chips = h('div', { class: 'chips' });
  const empty = h('div', { class: 'empty' }, emptyIcon, emptyTitle, emptyText, chips);
  const results = h('section', { class: 'card scan-results' }, head, list, empty);

  const el = h('section', { class: 'page', id: 'page-scan' }, bar.el, h('main', { class: 'body vert' }, toolbar, results));

  // ---------------------------------------------------------------- helpers
  const running = () => scan?.state === 'running';
  const isActive = () => el.classList.contains('active') && session.loggedIn;

  function current() {
    const p = prefixFromText(fPfx.value);
    if (!parseIPv4(fNet.value) || p == null || p < 1) return null;
    const net = networkOf(fNet.value, p);
    const local = networks.find((n) => n.prefix <= p && networkOf(net, n.prefix) === n.network);
    return { net, prefix: p, count: p >= 31 ? 2 ** (32 - p) : 2 ** (32 - p) - 2, local };
  }

  function changed() {
    dirty = true;
    formError = '';
    fNet.setError('');
    fPfx.setError('');
    render();
  }

  function setRange(network, prefix) {
    fNet.value = network;
    fPfx.value = String(prefix);
    changed();
  }

  // ---------------------------------------------------------------- table
  function makeRow(host) {
    const nameT = h('span', { class: 't' });
    const tags = h('span', { class: 'tags' });
    const cells = {
      name: h('span', { class: 'nm' }, nameT, tags), nameT, tags,
      ip: h('span', { class: 'ip' }), mac: h('span', { class: 'mac' }), ven: h('span', { class: 'ven' }),
    };
    const r = { cells, host };
    r.el = h('button', { class: 'scan-row', type: 'button', onclick: () => detail(r.host) },
      cells.name, cells.ip, cells.mac, cells.ven);
    return r;
  }

  function fillRow(r, host) {
    r.host = host;
    const c = r.cells;
    setText(c.nameT, host.name || '—');
    c.name.classList.toggle('none', !host.name);
    c.tags.replaceChildren(...(host.tags || []).map((t) => (t === 'self'
      ? h('span', { class: 'badge info' }, 'this')
      : h('span', { class: 'badge ok' }, 'gw'))));
    setText(c.ip, host.ip);
    setText(c.mac, (host.mac || '').toUpperCase() || '—');
    setText(c.ven, host.random_mac ? 'Private MAC' : host.vendor || 'Unknown');
    c.ven.classList.toggle('priv', host.random_mac || !host.vendor);
    r.el.classList.toggle('self', (host.tags || []).includes('self'));
  }

  function renderHosts(hosts) {
    const seen = new Set();
    let prev = null;
    for (const host of hosts) {
      seen.add(host.ip);
      let r = rows.get(host.ip);
      if (!r) {
        r = makeRow(host);
        rows.set(host.ip, r);
      }
      fillRow(r, host);
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

  function clearHosts() {
    rows.clear();
    list.replaceChildren();
  }

  function detail(host) {
    const found = SOURCES.filter(([k]) => host.names?.[k]).map(([k, label]) => `${label}: ${host.names[k]}`);
    const role = (host.tags || []).map((t) => (t === 'self' ? 'This device (NetPanel)' : 'Default gateway')).join(', ');
    const rowsKv = [
      ['IP address', host.ip],
      ['MAC address', (host.mac || '').toUpperCase() || '—'],
      ['Vendor', host.random_mac ? 'none - private / random MAC' : host.vendor_full || 'not in the vendor database', 'wrap'],
      ['Name', host.name ? `${host.name} (${sourceLabel(host.name_source)})` : 'no name announced', 'wrap'],
    ];
    if (found.length > 1) rowsKv.push(['All names', found.join(' · '), 'wrap']);
    if (role) rowsKv.push(['Role', role]);
    info({
      title: host.name || host.ip, iconName: 'radar',
      extra: [kvList(rowsKv), host.random_mac ? h('div', { class: 'note' }, icon('info'),
        h('span', null, 'Locally administered (randomised) MAC - typical for phones with "private Wi-Fi address". The vendor cannot be determined.')) : null],
    });
  }

  // ---------------------------------------------------------------- status / empty state
  function render() {
    const s = scan;
    const c = current();
    const busy = running();
    fNet.setReadonly(busy);
    fPfx.setReadonly(busy);

    goBtn.replaceChildren(icon(busy ? 'stop' : 'radar'), busy ? 'Stop' : 'Scan');
    goBtn.classList.toggle('stop', busy);
    goBtn.disabled = starting;

    progress.classList.toggle('hidden', !busy);
    progress.classList.toggle('indet', busy && s.phase === 'names');
    line1.classList.toggle('bad', !busy && (!!formError || s?.state === 'error'));

    if (formError) {
      setText(line1, 'Cannot scan this range');
      setText(line2, 'See the message below');
    } else if (busy && s.phase === 'names') {
      setText(line1, `Looking up names… ${s.count} found`);
      setText(line2, s.range);
    } else if (busy) {
      setText(line1, `Scanning ${s.probed} / ${s.total}`);
      setText(line2, `${s.count} found · ${s.range} on ${s.iface}`);
      barFill.style.width = `${s.total ? Math.round((100 * s.probed) / s.total) : 0}%`;
    } else if (s && s.state === 'error') {
      setText(line1, 'Scan failed');
      setText(line2, s.message || '');
    } else if (s && (s.state === 'done' || s.state === 'stopped') && !dirty) {
      setText(line1, `${s.count} device${s.count === 1 ? '' : 's'}${s.state === 'stopped' ? ' · stopped' : ''}`);
      setText(line2, s.vendor_db === false ? 'No vendor database: apt install ieee-data'
        : `${s.range} · ${hhmm(s.finished)} · ${s.duration} s`);
    } else if (c) {
      setText(line1, `${c.count.toLocaleString('en')} addresses`);
      setText(line2, c.local ? `${c.local.iface} · tap Scan to start` : 'not a directly connected network');
    } else {
      setText(line1, 'Enter a range');
      setText(line2, 'network address / prefix');
    }

    // results or empty state
    const hosts = s?.hosts || [];
    const showList = hosts.length > 0 && !formError;
    head.classList.toggle('hidden', !showList);
    list.classList.toggle('hidden', !showList);
    empty.classList.toggle('hidden', showList);
    empty.classList.toggle('bad', !!formError);
    if (showList) renderHosts(hosts);
    else renderEmpty(s);
  }

  function renderEmpty(s) {
    let ic = 'radar';
    let title = 'No scan yet';
    let text = 'Finds every device that answers ARP in a directly connected network - also PCs whose firewall blocks ping.';
    if (formError) {
      ic = 'warn';
      title = 'Cannot scan this range';
      text = formError;
    } else if (running()) {
      title = 'Scanning…';
      text = 'Devices appear here as they answer.';
    } else if (s && (s.state === 'done' || s.state === 'stopped')) {
      title = 'No devices found';
      text = `Nothing answered in ${s.range}.`;
    } else if (s && s.state === 'error') {
      ic = 'warn';
      title = 'Scan failed';
      text = s.message || '';
    }
    emptyIcon.replaceChildren(icon(ic));
    setText(emptyTitle, title);
    setText(emptyText, text);
    chips.replaceChildren(...networks.map((n) => h('button', {
      class: 'btn', type: 'button',
      onclick: () => {
        if (running()) return;
        if (n.prefix >= MIN_PREFIX) setRange(n.network, n.prefix);
        else setRange(networkOf(n.address, 24), 24); // big network: the /24 around this device
      },
    }, icon(n.iface.startsWith('w') ? 'wifi' : 'ethernet'), `${n.iface} · ${n.cidr}`)));
    chips.classList.toggle('hidden', running() || !networks.length);
  }

  // ---------------------------------------------------------------- actions
  async function startOrStop() {
    if (running()) return stop();
    const e1 = V.ip(fNet.value);
    const e2 = checkPrefix(fPfx.value);
    fNet.setError(e1);
    fPfx.setError(e2);
    if (e1 || e2) {
      formError = e1 || e2;
      return render();
    }
    const c = current();
    if (c.net !== fNet.value.trim()) fNet.value = c.net; // clear host bits: 192.168.1.57/24 -> 192.168.1.0
    starting = true;
    render();
    try {
      const r = await api('scan/start', { network: c.net, prefix: c.prefix });
      formError = '';
      dirty = false;
      clearHosts();
      scan = r.scan;
    } catch (e) {
      if (e.authRequired) return;
      if (e.status === 400) {
        fNet.setError(e.errors?.network || '');
        fPfx.setError(e.errors?.prefix || '');
        formError = e.message;
      } else toast(e.message, 'err');
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
      scan = (await api('scan/stop', {})).scan;
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
          scan = (await api('scan/status')).scan;
          render();
        } catch (e) {
          if (e.authRequired) break; // logged out: the app returns to home
        }
      }
    } finally {
      polling = false;
    }
  }

  async function load() {
    try {
      const r = await api('scan');
      scan = r.scan;
      networks = r.networks || [];
      suggested = r.suggested;
      if (!dirty) {
        if (scan && scan.range) {
          const [n, p] = scan.range.split('/');
          fNet.value = n;
          fPfx.value = p;
        } else if (suggested) {
          fNet.value = suggested.network;
          fPfx.value = String(suggested.prefix);
          if (suggested.note) toast(suggested.note, 'info');
        }
      }
      if (r.error) toast(r.error, 'err');
      render();
      poll();
    } catch (e) {
      if (!e.authRequired) toast(e.message, 'err');
    }
  }

  return {
    el,
    keepOnBack: false,
    enter() {
      dirty = false;
      formError = '';
      fNet.setError('');
      fPfx.setError('');
      render();
      load();
    },
  };
}
