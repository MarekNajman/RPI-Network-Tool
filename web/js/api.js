// JSON API client. POSTs carry the X-NetPanel header required by the server.

export class ApiError extends Error {
  constructor(message, { status = 0, errors = {}, offline = false } = {}) {
    super(message);
    this.status = status;
    this.errors = errors;
    this.offline = offline;
  }
}

export async function api(path, body) {
  const opts = body === undefined
    ? { cache: 'no-store' }
    : {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-NetPanel': '1' },
        body: JSON.stringify(body),
      };
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
    throw new ApiError(data.message || `Request failed (HTTP ${res.status})`, {
      status: res.status,
      errors: data.errors || {},
    });
  }
  return data;
}
