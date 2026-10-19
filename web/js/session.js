// Admin session: PIN login, logout, 5-minute inactivity auto-logout.
//
// The UI is authoritative for the idle timer (it sees every touch); while the
// user is active it pings the server so the server-side session (backstop,
// slightly longer timeout) stays alive.

import { api, setToken } from './api.js';

const PING_EVERY_MS = 30000;
export const WARN_BEFORE_MS = 30000;

export const session = {
  token: null,
  idleMs: 300000,
  lastActivity: Date.now(),
  lastPing: 0,
  configured: true,
  demo: false,
  _listeners: [],

  get loggedIn() { return !!this.token; },

  onChange(fn) { this._listeners.push(fn); },
  _emit(reason) { for (const fn of this._listeners) fn(this.loggedIn, reason); },

  async info() {
    const r = await api('auth');
    this.configured = r.configured;
    this.demo = !!r.demo;
    this.idleMs = (r.idle_timeout || 300) * 1000;
    return r;
  },

  async login(pin) {
    const r = await api('auth/login', { pin });
    this.token = r.token;
    setToken(r.token);
    this.idleMs = (r.idle_timeout || 300) * 1000;
    this.touch();
    this.lastPing = Date.now();
    this._emit('login');
  },

  logout(reason = 'manual') {
    if (!this.token) return;
    api('auth/logout', {}).catch(() => {}); // token header captured synchronously
    this.token = null;
    setToken(null);
    this._emit(reason);
  },

  touch() { this.lastActivity = Date.now(); },

  remainingMs() { return this.idleMs - (Date.now() - this.lastActivity); },

  // Called every second by the app.
  tick() {
    if (!this.token) return;
    const now = Date.now();
    if (this.remainingMs() <= 0) {
      this.logout('idle');
      return;
    }
    // keep the server session alive only while the user is actually active
    if (now - this.lastPing >= PING_EVERY_MS && now - this.lastActivity < PING_EVERY_MS + 5000) {
      this.lastPing = now;
      api('auth/ping', {}).catch(() => {}); // a 401 triggers the global handler
    }
  },
};
