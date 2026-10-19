// NetPanel web UI - router, status polling, kiosk hardening.

import { api } from './api.js';
import { h, icon } from './dom.js';
import { createHome } from './pages/home.js';
import { createEth } from './pages/eth.js';
import { createWifi } from './pages/wifi.js';
import { createStation } from './pages/station.js';
import { createTailscale } from './pages/tailscale.js';

const POLL_MS = 4000;

const app = {
  status: null,
  pages: {},
  current: 'home',
  history: [],

  go(name, params = {}, { replace = false } = {}) {
    const page = this.pages[name];
    if (!page) return;
    if (!replace && this.current !== name) this.history.push(this.current);
    this._show(name, params);
  },

  back() {
    const prev = this.history.pop() || 'home';
    this._show(prev, {}, true);
  },

  home() {
    this.history = [];
    this._show('home', {});
  },

  _show(name, params, isBack = false) {
    for (const [n, p] of Object.entries(this.pages)) p.el.classList.toggle('active', n === name);
    this.current = name;
    const page = this.pages[name];
    if (page.enter && !(isBack && page.keepOnBack)) page.enter(params);
    if (this.status && page.update) page.update(this.status);
  },

  async refresh() {
    try {
      const s = await api('status');
      this.status = s;
      offline(false);
      for (const p of Object.values(this.pages)) if (p.update && (p === this.pages.home || p.el.classList.contains('active'))) p.update(s);
    } catch (e) {
      offline(!!e.offline);
    }
  },
};

// ------------------------------------------------------------------ offline banner
let failures = 0;
let banner = null;
function offline(isOffline) {
  failures = isOffline ? failures + 1 : 0;
  const show = failures >= 2;
  if (show && !banner) {
    banner = h('div', { class: 'offline-banner' }, icon('warn'), 'NetPanel service is not responding - retrying…');
    document.body.append(banner);
  } else if (!show && banner) {
    banner.remove();
    banner = null;
  }
}

// ------------------------------------------------------------------ kiosk hardening
function harden() {
  // No context menu (long press), no pinch/ctrl-zoom, no drag ghosts, no text selection.
  document.addEventListener('contextmenu', (e) => e.preventDefault());
  document.addEventListener('dragstart', (e) => e.preventDefault());
  document.addEventListener('selectstart', (e) => e.preventDefault());
  document.addEventListener('wheel', (e) => { if (e.ctrlKey) e.preventDefault(); }, { passive: false });
  document.addEventListener('touchmove', (e) => { if (e.touches.length > 1) e.preventDefault(); }, { passive: false });
  document.addEventListener('keydown', (e) => {
    // Block browser shortcuts that could navigate away / zoom (USB keyboard).
    const k = e.key.toLowerCase();
    if ((e.ctrlKey && ['+', '-', '=', '0', 'r', 'w', 'n', 't', 'l', 'p', 's', 'o', 'u'].includes(k)) || k === 'f5' || k === 'f11' || k === 'f12'
      || (e.altKey && (k === 'arrowleft' || k === 'arrowright' || k === 'home'))) {
      e.preventDefault();
    }
    if (k === 'escape' && !document.querySelector('#layer > *') && app.current !== 'home') app.back();
  });
  // Swallow history navigation (e.g. browser back gesture).
  history.pushState(null, '', location.href);
  window.addEventListener('popstate', () => history.pushState(null, '', location.href));
}

// ------------------------------------------------------------------ boot
function boot() {
  harden();
  const root = document.getElementById('app');
  app.pages = {
    home: createHome(app),
    eth: createEth(app),
    wifi: createWifi(app),
    station: createStation(app),
    ts: createTailscale(app),
  };
  for (const p of Object.values(app.pages)) root.append(p.el);
  app._show('home', {});
  app.refresh();
  setInterval(() => { if (!document.hidden) app.refresh(); }, POLL_MS);
  window.netpanel = app; // handy for debugging via remote devtools / tests
}

boot();
