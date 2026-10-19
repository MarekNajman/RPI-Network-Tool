// Tailscale: status (left) + advertise subnet route (right).
// Applying runs: sudo tailscale set --advertise-routes=<network>/<prefix>

import { api } from '../api.js';
import { h, icon, setPill, setText, kvList } from '../dom.js';
import { tsPill, tsIPv4, routeApproved } from '../status.js';
import { Field, confirm, pageBar, runAction, spinWhile, toast } from '../ui.js';
import { V, parseIPv4, prefixFromText, networkOf, maskFromPrefix, hostCount } from '../validate.js';

const PREFIX_PRESETS = [
  { value: '24', label: '/24' }, { value: '16', label: '/16' }, { value: '8', label: '/8' },
  { value: '23', label: '/23' }, { value: '25', label: '/25' }, { value: '32', label: '/32 (host)' },
];

export function createTailscale(app) {
  let ts = null;
  let suggested = null;
  let dirty = false;

  const bar = pageBar({ app, iconName: 'vpn', color: 'var(--ts)', title: 'Tailscale', sub: 'Subnet router', onRefresh: () => load(true) });

  // ---- left: status
  const statusPill = h('span', { class: 'pill off' }, 'Loading…');
  const statusKv = h('div');
  const left = h('section', { class: 'card' }, h('h3', null, 'Status'), statusPill, statusKv);
  left.style.width = '262px';
  left.style.flex = 'none';

  // ---- right: advertise
  const fNet = new Field({ label: 'Network address', kind: 'ip', placeholder: '192.168.1.0', onChange: changed });
  const fPfx = new Field({ label: 'Prefix', kind: 'prefix', placeholder: '24', presets: PREFIX_PRESETS, onChange: changed });
  const info = h('div', { class: 'hint' });
  const useEthBtn = h('button', { class: 'btn', type: 'button', onclick: useSuggested }, icon('ethernet'), 'Use eth0 subnet');
  useEthBtn.style.height = '46px';
  const preview = h('div', { class: 'code' });
  const warnBox = h('div', { class: 'note warn hidden' }, icon('warn'), h('span'));
  const stopBtn = h('button', { class: 'btn danger', type: 'button', onclick: stopAdvertising }, icon('close'), 'Stop advertising');
  const advBtn = h('button', { class: 'btn violet', type: 'button', onclick: advertise }, icon('subnet'), 'Advertise');
  const right = h('section', { class: 'card' },
    h('h3', null, 'Advertise subnet'),
    h('div', { class: 'grid2 ts-grid' }, fNet.el, fPfx.el),
    h('div', { class: 'footer' }, info, useEthBtn),
    preview, warnBox,
    h('div', { class: 'footer' }, stopBtn, h('div', { class: 'hint' }), advBtn));
  right.style.flex = '1';

  const el = h('section', { class: 'page', id: 'page-ts' }, bar.el, h('main', { class: 'body' }, left, right));

  // ---------------------------------------------------------------- helpers
  function current() {
    const p = prefixFromText(fPfx.value);
    if (!parseIPv4(fNet.value) || p == null || p < 1) return null;
    const net = networkOf(fNet.value, p);
    return { route: `${net}/${p}`, prefix: p, changed: net !== fNet.value.trim() };
  }

  function changed() {
    dirty = true;
    fNet.setError('');
    fPfx.setError('');
    renderPreview();
  }

  function renderPreview() {
    const c = current();
    if (!c) {
      setText(preview, 'sudo tailscale set --advertise-routes=…');
      setText(info, 'Enter a network address and prefix.');
      return;
    }
    setText(preview, `sudo tailscale set --advertise-routes=${c.route}`);
    const src = suggested && suggested.route === c.route ? ` · subnet of ${suggested.source}` : '';
    const fix = c.changed ? ` · host bits cleared → ${c.route}` : '';
    setText(info, `Mask ${maskFromPrefix(c.prefix)} · ${hostCount(c.prefix).toLocaleString('en')} hosts${src}${fix}`);
  }

  function useSuggested() {
    if (!suggested) {
      toast('eth0 has no IPv4 address yet.', 'info');
      return;
    }
    fNet.value = suggested.network;
    fPfx.value = String(suggested.prefix);
    changed();
    dirty = false;
  }

  function renderStatus() {
    setPill(statusPill, ...tsPill(ts));
    const adv = ts?.advertised_routes;
    const advNode = !adv ? '—' : !adv.length ? 'None' : h('span', null, adv.join(', '),
      h('span', { class: `badge ${adv.every((r) => routeApproved(ts, r)) ? 'ok' : 'warn'}` },
        adv.every((r) => routeApproved(ts, r)) ? 'approved' : 'pending'));
    statusKv.replaceChildren(kvList([
      ['Tailscale IP', tsIPv4(ts) || '—'],
      ['Device', ts?.dns_name || ts?.hostname || '—', 'wrap'],
      ['Advertised', advNode, 'wrap'],
      ['Active', (ts?.primary_routes || []).join(', ') || '—', 'wrap'],
      ['IP forwarding', ts?.ip_forward == null ? '—' : ts.ip_forward ? 'enabled' : 'DISABLED'],
    ], true));

    // warnings, most important first
    let warn = '';
    if (ts && !ts.installed) warn = 'Tailscale is not installed. See README → Tailscale.';
    else if (ts && !ts.daemon) warn = 'tailscaled is not running: sudo systemctl enable --now tailscaled';
    else if (ts && ts.backend_state === 'NeedsLogin') warn = 'Not logged in. Run “sudo tailscale up” once over SSH, then advertise.';
    else if (ts && ts.ip_forward === false) warn = 'IP forwarding is disabled - routed traffic will not pass. Re-run the installer.';
    warnBox.classList.toggle('hidden', !warn);
    setText(warnBox.querySelector('span:last-child'), warn);

    const usable = ts && ts.installed && ts.daemon;
    advBtn.disabled = !usable;
    stopBtn.disabled = !usable || !(adv && adv.length);
  }

  async function load(force = false) {
    try {
      const r = await spinWhile(bar.refresh, api('tailscale'));
      ts = r.tailscale;
      suggested = r.suggested;
      renderStatus();
      if (force || !dirty) {
        const adv = ts.advertised_routes || [];
        if (adv.length && adv[0].includes('.')) {
          const [n, p] = adv[0].split('/');
          fNet.value = n;
          fPfx.value = p;
        } else if (suggested) {
          fNet.value = suggested.network;
          fPfx.value = String(suggested.prefix);
        }
        dirty = false;
        renderPreview();
      }
      setText(useEthBtn.lastChild, suggested ? `Use ${suggested.source} subnet` : 'Use eth0 subnet');
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function advertise() {
    const e1 = V.ip(fNet.value);
    const e2 = V.prefix(fPfx.value);
    fNet.setError(e1);
    fPfx.setError(e2);
    const c = current();
    if (e1 || e2 || !c) return;
    if (c.changed) {
      fNet.value = c.route.split('/')[0];
      renderPreview();
    }
    const prev = (ts?.advertised_routes || []).filter((r) => r !== c.route);
    const ok = await confirm({
      title: 'Advertise subnet?', iconName: 'subnet', ok: 'Advertise', okClass: 'violet',
      code: `sudo tailscale set --advertise-routes=${c.route}`,
      text: `${prev.length ? `Replaces the currently advertised ${prev.join(', ')}. ` : ''}`
        + 'Tailscale peers can then reach this subnet through this device once the route is approved in the admin console.',
    });
    if (!ok) return;
    const res = await runAction({
      busyText: 'Updating Tailscale routes…',
      fn: () => api('tailscale/routes', { routes: [c.route] }),
      errorTitle: 'tailscale set failed',
      onFieldErrors: (er) => { fNet.setError(er.network); fPfx.setError(er.prefix); },
    });
    if (res) {
      dirty = false;
      await load(true);
      app.refresh();
    }
  }

  async function stopAdvertising() {
    const ok = await confirm({
      title: 'Stop advertising routes?', danger: true, ok: 'Stop',
      code: 'sudo tailscale set --advertise-routes=',
      text: 'Tailscale peers lose access to the local subnet through this device.',
    });
    if (!ok) return;
    const res = await runAction({ busyText: 'Updating Tailscale routes…', fn: () => api('tailscale/routes', { clear: true }) });
    if (res) {
      await load(true);
      app.refresh();
    }
  }

  return {
    el,
    enter() {
      dirty = false;
      if (app.status?.tailscale) {
        ts = app.status.tailscale;
        renderStatus();
      }
      load(true);
    },
    update(s) {
      if (!s.tailscale) return;
      ts = s.tailscale;
      renderStatus();
    },
  };
}
