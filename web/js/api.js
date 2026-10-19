// JSON API client. POSTs carry the X-NetPanel header required by the server;
// when logged in, every request carries the session token (kept in memory only,
// so reloading the page = logged out).

export class ApiError extends Error {
  constructor(message, { status = 0, errors = {}, offline = false, authRequired = false, retryAfter = 0, attemptsLeft = null } = {}) {
    super(message);
    this.status = status;
    this.errors = errors;
    this.offline = offline;
    this.authRequired = authRequired;
    this.retryAfter = retryAfter;
    this.attemptsLeft = attemptsLeft;
  }
}

let token = null;
let onAuthRequired = null;

export const setToken = (t) => { token = t || null; };
export const getToken = () => token;
// Called when the server answers 401 (no / expired session).
export const setAuthHandler = (fn) => { onAuthRequired = fn; };

export async function api(path, body) {
  const headers = {};
  if (token) headers['X-NetPanel-Token'] = token;
  const opts = { cache: 'no-store', headers };
  if (body !== undefined) {
    opts.method = 'POST';
    headers['Content-Type'] = 'application/json';
    headers['X-NetPanel'] = '1';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`/api/${path}`, opts);
  } catch (e) {
    throw new ApiError('The NetPanel service is not responding.', { offline: true });
  }
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    /* non-JSON error body */
  }
  if (!res.ok || data.ok === false) {
    const err = new ApiError(data.message || `Request failed (HTTP ${res.status})`, {
      status: res.status,
      errors: data.errors || {},
      authRequired: res.status === 401 && !!data.auth_required,
      retryAfter: data.retry_after || 0,
      attemptsLeft: data.attempts_left ?? null,
    });
    if (err.authRequired && onAuthRequired) onAuthRequired();
    throw err;
  }
  return data;
}
