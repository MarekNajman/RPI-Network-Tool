// Home: Ethernet + Wi-Fi tiles on top, Tailscale section below.

import { h, icon, setText, setPill, cidr } from '../dom.js';
import { ethPill, ethMode, wifiPill, wifiMode, tsPill, tsIPv4, routeApproved } from '../status.js';

function makeTile({ cls, iconName, name, labels, onTap }) {
  const sub = h('div', { class: 'tile-sub' }, '…');
  const pill = h('span', { class: 'pill off' }, 'Loading…');
  const dds = labels.map(() => h('dd', null, '—'));
  const dl = h('dl', { class: 'kv' });
  labels.forEach((l, i) => dl.append(h('dt', null, l), dds[i]));
  dds[0].classList.add('big');
  const el = h('button', { class: `tile ${cls}`, type: 'button', onclick: onTap },
    h('div', { class: 'tile-head' },
      h('span', { class: 'ibox' }, icon(iconName)),
      h('div', null, h('div', { class: 'tile-name' }, name), sub),
      icon('chevron', 'go')),
    pill, dl);
  return { el, sub, pill, dds };
}

export function createHome(app) {
  const host = h('span', { class: 'host' });
  const demo = h('span', { class: 'chip demo hidden' }, 'DEMO');
  const tempTxt = h('span', null, '--');
  const temp = h('span', { class: 'chip num' }, icon('temp'), tempTxt);
  const clock = h('span', { class: 'clock num' });
  const top = h('header', { class: 'topbar' },
    h('div', { class: 'brand' }, icon('logo'), 'NetPanel'), host,
    h('div', { class: 'spacer' }), demo, temp, clock);

  const eth = makeTile({ cls: 'eth', iconName: 'ethernet', name: 'Ethernet', labels: ['IP', 'Gateway'], onTap: () => app.go('eth') });
  const wifi = makeTile({ cls: 'wifi', iconName: 'wifi', name: 'Wi-Fi', labels: ['IP', 'SSID'], onTap: () => app.go('wifi') });

  // Tailscale section
  const tsSub = h('div', { class: 'tile-sub' }, 'Subnet router');
  const tsPillEl = h('span', { class: 'pill off' }, 'Loading…');
  const tsIp = h('dd', { class: 'big' }, '—');
  const tsAdv = h('dd', null, '—');
  const tsInfo = h('div', { class: 'ts-info' },
    h('div', { class: 'tile-head' },
      h('span', { class: 'ibox' }, icon('vpn')),
      h('div', null, h('div', { class: 'tile-name' }, 'Tailscale'), tsSub)),
    tsPillEl,
    h('dl', { class: 'kv' }, h('dt', null, 'Tailscale IP'), tsIp, h('dt', null, 'Advertised'), tsAdv));
  const advBtn = h('button', { class: 'adv-btn', type: 'button', onclick: () => app.go('ts') },
    icon('subnet'), h('b', null, 'Advertise subnet'), h('span', { class: 's' }, 'Share LAN via Tailscale'));
  const tsCard = h('section', { class: 'ts-card' }, tsInfo, advBtn);

  const el = h('section', { class: 'page', id: 'page-home' },
    top, h('main', { class: 'home' }, h('div', { class: 'tiles' }, eth.el, wifi.el), tsCard));

  function tick() {
    const d = new Date();
    setText(clock, `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`);
  }
  tick();
  setInterval(tick, 1000);

  function update(s) {
    if (!s) return;
    setText(host, s.system?.hostname || '');
    demo.classList.toggle('hidden', !s.system?.demo);
    setText(tempTxt, s.system?.cpu_temp != null ? `${Math.round(s.system.cpu_temp)}°C` : '--');

    const e = s.eth;
    setText(eth.sub, `${e?.iface || 'eth0'} · ${ethMode(e)}`);
    setPill(eth.pill, ...ethPill(e, s.eth_error));
    setText(eth.dds[0], e ? cidr(e.address, e.prefix) : '—');
    setText(eth.dds[1], e?.gateway || '—');

    const w = s.wifi;
    setText(wifi.sub, `${w?.iface || 'wlan0'} · ${wifiMode(w)}`);
    setPill(wifi.pill, ...wifiPill(w, s.wifi_error));
    setText(wifi.dds[0], w ? cidr(w.address, w.prefix) : '—');
    setText(wifi.dds[1], w?.ssid || '—');

    const t = s.tailscale;
    setPill(tsPillEl, ...tsPill(t, s.tailscale_error));
    setText(tsSub, t?.dns_name || t?.hostname || 'Subnet router');
    setText(tsIp, tsIPv4(t) || '—');
    const routes = t?.advertised_routes;
    tsAdv.replaceChildren();
    if (!routes || !routes.length) tsAdv.append(routes ? 'None' : '—');
    else {
      tsAdv.append(routes.join(', '));
      const ok = routes.every((r) => routeApproved(t, r));
      tsAdv.append(h('span', { class: `badge ${ok ? 'ok' : 'warn'}` }, ok ? 'approved' : 'pending'));
    }
  }

  return { el, update };
}
