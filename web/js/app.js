// NetPanel web UI - router, status polling, admin session, kiosk hardening.

import { api, setAuthHandler } from './api.js';
import { h, icon, setText } from './dom.js';
import { showLogin } from './login.js';
import { session, WARN_BEFORE_MS } from './session.js';
import { toast } from './ui.js';
import { createHome } from './pages/home.js';
import { createEth } from './pages/eth.js';
import { createWifi } from './pages/wifi.js';
import { createStation } from './pages/station.js';
import { createTailscale } from './pages/tailscale.js';

const POLL_MS = 4000;
const PUBLIC_PAGES = new Set(['home']); // everything else needs the admin PIN

const app = {
  status: null,
  pages: {},
  current: 'home',
  history: [],
  session,

  async go(name, params = {}, { replace = false } = {}) {
    const page = this.pages[name];
    if (!page) return;
    if (!PUBLIC_PAGES.has(name) && !session.loggedIn) {
      if (!(await this.login())) return;
    }
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

  async login(reason = '') {
    const ok = await showLogin({ reason });
    if (ok) {
      toast('Logged in. Auto logout after 5 min of inactivity.', 'info');
      this.refresh();
    }
    return ok;
  },

  logout(reason = 'manual') {
    session.logout(reason);
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
      // Session dropped on the server (service restart, PIN changed) -> log out here too.
      if (session.loggedIn && s.auth && !s.auth.logged_in) session.logout('expired');
      for (const p of Object.values(this.pages)) if (p.update && (p === this.pages.home || p.el.classList.contains('active'))) p.update(s);
    } catch (e) {
      offline(!!e.offline);
    }
  },
};

// ------------------------------------------------------------------ session handling
const LOGOUT_TEXT = {
  manual: 'Logged out.',
  idle: 'Logged out after 5 minutes of inactivity.',
  expired: 'Session expired - please log in again.',
};

session.onChange((loggedIn, reason) => {
  if (!loggedIn) {
    // Close every editor/dialog and return to the public overview.
    window.dispatchEvent(new Event('netpanel-reset'));
    document.getElementById('layer').replaceChildren();
    idleBanner(false);
    app.home();
    toast(LOGOUT_TEXT[reason] || LOGOUT_TEXT.manual, reason === 'manual' ? 'info' : 'err');
  }
  app.pages.home.updateSession();
});

// Any 401 from the API = our session is gone.
setAuthHandler(() => { if (session.loggedIn) session.logout('expired'); });

let banner = null;
let bannerText = null;
function idleBanner(seconds) {
  if (seconds === false) {
    if (banner) { banner.remove(); banner = null; }
    return;
  }
  if (!banner) {
    bannerText = h('span');
    banner = h('div', { class: 'idle-banner' }, icon('lock'), bannerText);
    document.body.append(banner);
  }
  setText(bannerText, `Auto logout in ${seconds} s - touch the screen to stay logged in`);
}

function sessionTick() {
  session.tick();
  if (!session.loggedIn) return;
  const left = session.remainingMs();
  if (left <= WARN_BEFORE_MS) idleBanner(Math.max(1, Math.ceil(left / 1000)));
  else idleBanner(false);
  app.pages.home.updateSession();
}

function trackActivity() {
  const touch = () => {
    if (!session.loggedIn) return;
    session.touch();
    if (banner) idleBanner(false);
  };
  for (const ev of ['pointerdown', 'keydown', 'touchstart']) document.addEventListener(ev, touch, { capture: true, passive: true });
}

// ------------------------------------------------------------------ offline banner
let failures = 0;
let offBanner = null;
function offline(isOffline) {
  failures = isOffline ? failures + 1 : 0;
  const show = failures >= 2;
  if (show && !offBanner) {
    offBanner = h('div', { class: 'offline-banner' }, icon('warn'), 'NetPanel service is not responding - retrying…');
    document.body.append(offBanner);
  } else if (!show && offBanner) {
    offBanner.remove();
    offBanner = null;
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
  trackActivity();
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
  app.pages.home.updateSession();
  session.info().catch(() => {});
  app.refresh();
  setInterval(() => { if (!document.hidden) app.refresh(); }, POLL_MS);
  setInterval(sessionTick, 1000);
  window.netpanel = app; // handy for debugging via remote devtools / tests
}

boot();
