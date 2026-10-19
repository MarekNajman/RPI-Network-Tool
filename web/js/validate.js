// Client-side validation for instant feedback. The server re-validates everything.

export function parseIPv4(s) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec((s || '').trim());
  if (!m) return null;
  const parts = m.slice(1).map((x) => (x.length > 1 && x[0] === '0' ? NaN : Number(x)));
  return parts.every((n) => n >= 0 && n <= 255) ? parts : null;
}

export const ipToInt = (p) => ((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3];
export const intToIp = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.');

export function prefixFromText(s) {
  s = String(s ?? '').trim().replace(/^\//, '');
  if (!s) return null;
  if (s.includes('.')) {
    const p = parseIPv4(s);
    if (!p) return null;
    const n = ipToInt(p);
    const inv = ~n >>> 0;
    if (((inv & (inv + 1)) >>> 0) !== 0) return null; // non-contiguous mask
    let bits = 0;
    for (let i = 31; i >= 0 && (n >>> i) & 1; i--) bits++;
    return bits;
  }
  if (!/^\d{1,2}$/.test(s)) return null;
  const v = Number(s);
  return v <= 32 ? v : null;
}

export const maskFromPrefix = (p) => (p === 0 ? '0.0.0.0' : intToIp((0xffffffff << (32 - p)) >>> 0));

export function networkOf(ip, prefix) {
  const p = parseIPv4(ip);
  if (!p || prefix == null) return null;
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return intToIp((ipToInt(p) & mask) >>> 0);
}

export function hostCount(prefix) {
  if (prefix >= 31) return prefix === 31 ? 2 : 1;
  return 2 ** (32 - prefix) - 2;
}

function hostProblem(p) {
  const n = ipToInt(p);
  if (n === 0) return '0.0.0.0 is not a valid host address';
  if (p[0] === 127) return 'Loopback addresses are not allowed';
  if (p[0] >= 224) return 'Multicast/reserved address';
  if (p[0] === 169 && p[1] === 254) return 'Link-local addresses are not allowed';
  return '';
}

export function sameNet(a, b, prefix) {
  return networkOf(a, prefix) === networkOf(b, prefix);
}

// Validators: return '' when OK, otherwise a human readable message.
export const V = {
  ip(v) {
    const p = parseIPv4(v);
    if (!p) return 'Enter an IPv4 address like 192.168.1.10';
    return hostProblem(p);
  },
  mask(v) {
    const p = prefixFromText(v);
    if (p == null) return 'Enter a mask like 255.255.255.0 or /24';
    if (p < 1) return 'Mask /0 is not usable';
    return '';
  },
  prefix(v) {
    const p = prefixFromText(v);
    if (p == null || p < 1) return 'Prefix must be 1-32';
    return '';
  },
  cidr(v) {
    const [a, b, extra] = String(v || '').split('/');
    if (extra !== undefined || b === undefined) return 'Use address/prefix, e.g. 10.42.0.1/24';
    return V.ip(a) || V.prefix(b);
  },
  iplist(v) {
    const items = String(v || '').split(/[\s,]+/).filter(Boolean);
    if (items.length > 3) return 'At most 3 DNS servers';
    for (const it of items) if (!parseIPv4(it)) return `Invalid address: ${it}`;
    return '';
  },
  ssid(v) {
    const n = new TextEncoder().encode(v || '').length;
    if (!n) return 'SSID cannot be empty';
    return n > 32 ? 'SSID is longer than 32 bytes' : '';
  },
  psk(v) {
    v = v || '';
    if (v.length === 64 && /^[0-9a-f]+$/i.test(v)) return '';
    if (v.length < 8 || v.length > 63) return 'Password must have 8-63 characters';
    return /^[\x20-\x7e]*$/.test(v) ? '' : 'Only ASCII characters are allowed';
  },
};

// Validate a static IPv4 form. Returns {errors:{field:msg}, cfg}.
export function checkStatic({ address, mask, gateway, dns }) {
  const errors = {};
  const e1 = V.ip(address);
  if (e1) errors.address = e1;
  const e2 = V.mask(mask);
  if (e2) errors.mask = e2;
  const prefix = prefixFromText(mask);
  if (!e1 && !e2 && prefix <= 30) {
    const net = networkOf(address, prefix);
    const bcast = intToIp((ipToInt(parseIPv4(net)) | (~((0xffffffff << (32 - prefix)) >>> 0) >>> 0)) >>> 0);
    if (address.trim() === net || address.trim() === bcast) errors.address = `Network/broadcast address of ${net}/${prefix}`;
  }
  if (gateway) {
    const eg = V.ip(gateway);
    if (eg) errors.gateway = eg;
    else if (!errors.address && !errors.mask && !sameNet(address, gateway, prefix)) errors.gateway = 'Gateway must be in the same subnet';
    else if (gateway.trim() === address.trim()) errors.gateway = "Gateway can't be the device's own address";
  }
  const ed = V.iplist(dns);
  if (ed) errors.dns = ed;
  const dnsList = String(dns || '').split(/[\s,]+/).filter(Boolean);
  return {
    errors,
    cfg: { method: 'manual', address: address.trim(), prefix, gateway: (gateway || '').trim(), dns: dnsList },
  };
}
