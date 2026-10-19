// Ethernet: link status (left) + DHCP / static IPv4 configuration (right).

import { api } from '../api.js';
import { h, icon, setPill, setText, kvList, speedText } from '../dom.js';
import { ethPill, ethMode } from '../status.js';
import { Field, Segmented, confirm, pageBar, runAction, spinWhile, toast } from '../ui.js';
import { checkStatic, maskFromPrefix, parseIPv4, prefixFromText, networkOf, intToIp, ipToInt } from '../validate.js';

export const MASK_PRESETS = [
  { value: '255.255.255.0', label: '255.255.255.0  /24' },
  { value: '255.255.0.0', label: '255.255.0.0  /16' },
  { value: '255.0.0.0', label: '255.0.0.0  /8' },
  { value: '255.255.255.128', label: '/25' },
  { value: '255.255.254.0', label: '/23' },
];
export const DNS_PRESETS = ['1.1.1.1', '8.8.8.8', '9.9.9.9'];

// Suggest the usual ".1" gateway for the address/mask the user entered.
export function gatewayPresets(addrField, maskField) {
  const p = prefixFromText(maskField.value);
  if (!parseIPv4(addrField.value) || p == null || p > 30) return [];
  const net = networkOf(addrField.value, p);
  return [intToIp((ipToInt(parseIPv4(net)) + 1) >>> 0)];
}

// Four IPv4 fields used by both Ethernet and Wi-Fi station pages.
export function ipv4Fields() {
  const f = {};
  f.address = new Field({ label: 'IP address', kind: 'ip', placeholder: '192.168.1.10' });
  f.mask = new Field({ label: 'Subnet mask', kind: 'mask', placeholder: '255.255.255.0', presets: MASK_PRESETS });
  f.gateway = new Field({ label: 'Gateway', kind: 'ip', optional: true, presets: () => gatewayPresets(f.address, f.mask) });
  f.dns = new Field({ label: 'DNS servers', kind: 'iplist', optional: true, presets: DNS_PRESETS });
  f.all = [f.address, f.mask, f.gateway, f.dns];
  f.showErrors = (errors) => {
    f.address.setError(errors.address);
    f.mask.setError(errors.mask || errors.prefix);
    f.gateway.setError(errors.gateway);
    f.dns.setError(errors.dns);
  };
  f.values = () => ({ address: f.address.value, mask: f.mask.value, gateway: f.gateway.value, dns: f.dns.value });
  f.set = ({ address = '', prefix = null, netmask = '', gateway = '', dns = [] } = {}) => {
    f.address.value = address;
    f.mask.value = netmask || (prefix != null && address ? maskFromPrefix(prefix) : '');
    f.gateway.value = gateway || '';
    f.dns.value = (dns || []).join(', ');
  };
  return f;
}

export function createEth(app) {
  let eth = null;
  let mode = 'auto';
  let dirty = false;

  const bar = pageBar({ app, iconName: 'ethernet', color: 'var(--accent)', title: 'Ethernet', sub: 'eth0', onRefresh: () => load(true) });

  // ---- left: link
  const linkPill = h('span', { class: 'pill off' }, 'Loading…');
  const linkKv = h('div');
  const left = h('section', { class: 'card' }, h('h3', null, 'Link'), linkPill, linkKv);
  left.style.width = '262px';
  left.style.flex = 'none';

  // ---- right: IPv4
  const seg = new Segmented([{ value: 'auto', label: 'DHCP' }, { value: 'manual', label: 'Static IP' }], 'auto', onMode);
  const f = ipv4Fields();
  for (const x of f.all) x.onChange = () => { dirty = true; };
  const hint = h('div', { class: 'hint' });
  const applyBtn = h('button', { class: 'btn primary', type: 'button', onclick: apply }, icon('check'), 'Apply');
  const right = h('section', { class: 'card' },
    h('h3', null, 'IPv4 configuration'), seg.el,
    h('div', { class: 'grid2' }, f.address.el, f.mask.el, f.gateway.el, f.dns.el),
    h('div', { class: 'footer' }, hint, applyBtn));
  right.style.flex = '1';

  const el = h('section', { class: 'page', id: 'page-eth' }, bar.el, h('main', { class: 'body' }, left, right));

  function renderLink() {
    setText(bar.subEl, eth?.iface || 'eth0');
    setPill(linkPill, ...ethPill(eth));
    if (!eth) return;
    linkKv.replaceChildren(kvList([
      ['Mode', ethMode(eth)],
      ['Speed', eth.carrier && eth.speed ? `${speedText(eth.speed)}${eth.duplex ? ' · ' + eth.duplex : ''}` : '—'],
      ['MAC', eth.mac || '—'],
      ['Profile', eth.connection || '—'],
      ['DNS', (eth.dns || []).join(', ') || '—', 'wrap'],
    ]));
  }

  function fillLive() {
    f.set({ address: eth?.address, prefix: eth?.prefix, netmask: eth?.netmask, gateway: eth?.gateway, dns: eth?.dns });
  }

  function fillForMode() {
    const c = eth?.configured;
    if (mode === 'manual' && c && c.method === 'manual') f.set(c);
    else fillLive();
  }

  function setMode(m) {
    mode = m;
    seg.set(m);
    const ro = m === 'auto';
    for (const x of f.all) {
      x.setReadonly(ro);
      x.setError('');
    }
    f.address.setTag(ro ? 'from DHCP' : '');
    if (ro) {
      setText(hint, eth?.method === 'auto' ? 'Values received from the DHCP server.' : 'After Apply the address is requested from DHCP.');
    } else {
      setText(hint, 'Tap a field to edit it.');
    }
    hint.classList.remove('bad');
  }

  function onMode(m) {
    dirty = true;
    mode = m;
    fillForMode();
    setMode(m);
  }

  async function load(force = false) {
    try {
      const r = await spinWhile(bar.refresh, api('eth'));
      eth = r.eth;
      renderLink();
      if (force || !dirty) {
        dirty = false;
        mode = eth.method === 'manual' ? 'manual' : 'auto';
        fillForMode();
        setMode(mode);
      }
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function apply() {
    const iface = eth?.iface || 'eth0';
    let body;
    let dialog;
    if (mode === 'auto') {
      body = { method: 'auto' };
      dialog = {
        title: 'Switch Ethernet to DHCP?',
        text: `${iface} reconnects and requests a new address. Remote sessions over Ethernet may drop for a moment.`,
        ok: 'Use DHCP',
      };
    } else {
      const { errors, cfg } = checkStatic(f.values());
      f.showErrors(errors);
      if (Object.keys(errors).length) {
        setText(hint, 'Please fix the highlighted fields.');
        hint.classList.add('bad');
        return;
      }
      body = cfg;
      dialog = {
        title: 'Apply static IP?',
        text: `${iface} reconnects with these settings. Remote sessions over Ethernet may drop.`,
        code: `Address  ${cfg.address}/${cfg.prefix}\nGateway  ${cfg.gateway || '—'}\nDNS      ${cfg.dns.join(', ') || '—'}`,
        ok: 'Apply',
      };
    }
    if (!(await confirm(dialog))) return;
    const res = await runAction({
      busyText: 'Applying Ethernet settings…',
      busySub: 'This can take up to 40 seconds',
      fn: () => api('eth', body),
      errorTitle: 'Ethernet settings failed',
      onFieldErrors: f.showErrors,
    });
    if (res) {
      dirty = false;
      await load(true);
      app.refresh();
    }
  }

  return {
    el,
    enter() {
      dirty = false;
      eth = app.status?.eth || null;
      if (eth) {
        renderLink();
        mode = eth.method === 'manual' ? 'manual' : 'auto';
        fillForMode();
        setMode(mode);
      }
      load(true);
    },
    update(s) {
      if (!s.eth) return;
      eth = s.eth;
      renderLink();
      if (mode === 'auto' && !dirty) fillLive();
    },
  };
}
