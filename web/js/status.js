// Shared "status -> human text" helpers used by the home tiles and the pages.

import { speedText } from './dom.js';

export function ethPill(e, err) {
  if (!e) return ['err', err || 'Unavailable'];
  if (!e.present) return ['err', 'Interface not found'];
  if (!e.carrier) return ['err', 'Cable unplugged'];
  if (e.state === 'connected') return ['ok', `Connected${e.speed ? ' · ' + speedText(e.speed) : ''}`];
  if (e.state === 'connecting') return ['warn', 'Connecting…'];
  if (e.state === 'unmanaged') return ['warn', 'Not managed by NetworkManager'];
  return ['warn', 'Link up · no IP configuration'];
}

export const ethMode = (e) => (!e ? '—' : e.method === 'manual' ? 'Static IP' : e.method === 'auto' ? 'DHCP' : '—');

export function wifiPill(w, err) {
  if (!w) return ['err', err || 'Unavailable'];
  if (!w.present) return ['err', 'No Wi-Fi adapter'];
  if (!w.radio_hw) return ['err', 'Blocked (rfkill)'];
  if (!w.radio) return ['off', 'Radio off'];
  if (w.mode === 'ap') {
    const n = w.clients;
    return ['ok', n == null ? 'AP active' : `AP active · ${n} client${n === 1 ? '' : 's'}`];
  }
  if (w.mode === 'station' && w.state === 'connected') return ['ok', `Connected${w.signal != null ? ` · ${w.signal} %` : ''}`];
  if (w.state === 'connecting') return ['warn', 'Connecting…'];
  return ['warn', 'Not connected'];
}

export const wifiMode = (w) => (!w ? '—' : w.mode === 'ap' ? 'Access point' : w.mode === 'station' ? 'Station' : w.radio ? 'Station' : 'Off');

export const bandLabel = (b) => (b === 'a' ? '5 GHz' : b === 'bg' ? '2.4 GHz' : b || '');

export function secLabel(kind) {
  return {
    open: 'Open', 'wpa-psk': 'WPA2-Personal', sae: 'WPA3-Personal', enterprise: '802.1X Enterprise',
    wep: 'WEP', owe: 'Enhanced Open (OWE)', unsupported: 'Unsupported',
  }[kind] || kind;
}

export const secSupported = (kind) => ['open', 'wpa-psk', 'sae'].includes(kind);

export function tsPill(t, err) {
  if (!t) return ['err', err || 'Unavailable'];
  if (!t.installed) return ['err', 'Not installed'];
  if (!t.daemon) return ['err', 'tailscaled not running'];
  switch (t.backend_state) {
    case 'Running': return t.online === false ? ['warn', 'Running · offline'] : ['ok', 'Connected'];
    case 'NeedsLogin': return ['warn', 'Needs login'];
    case 'NeedsMachineAuth': return ['warn', 'Awaiting admin approval'];
    case 'Stopped': return ['off', 'Stopped'];
    case 'Starting': return ['warn', 'Starting…'];
    default: return ['warn', t.backend_state || 'Unknown'];
  }
}

export const tsIPv4 = (t) => (t && t.ips ? t.ips.find((x) => x.includes('.')) : '') || '';

export const routeApproved = (t, route) => !!(t && t.primary_routes && t.primary_routes.includes(route));
