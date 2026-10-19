// Run with: node tests/test_validate.mjs
import assert from 'node:assert/strict';
import { parseIPv4, prefixFromText, maskFromPrefix, networkOf, checkStatic, V, hostCount } from '../web/js/validate.js';

assert.deepEqual(parseIPv4('192.168.1.1'), [192, 168, 1, 1]);
assert.equal(parseIPv4('192.168.01.1'), null);
assert.equal(parseIPv4('256.1.1.1'), null);
assert.equal(prefixFromText('255.255.255.0'), 24);
assert.equal(prefixFromText('/16'), 16);
assert.equal(prefixFromText('255.0.255.0'), null);
assert.equal(prefixFromText('255.255.255.255'), 32);
assert.equal(maskFromPrefix(24), '255.255.255.0');
assert.equal(maskFromPrefix(1), '128.0.0.0');
assert.equal(networkOf('192.168.10.57', 24), '192.168.10.0');
assert.equal(hostCount(24), 254);
assert.ok(checkStatic({ address: '192.168.1.255', mask: '24', gateway: '', dns: '' }).errors.address);
assert.ok(checkStatic({ address: '192.168.1.10', mask: '24', gateway: '192.168.2.1', dns: '' }).errors.gateway);
const ok = checkStatic({ address: '192.168.1.10', mask: '255.255.255.0', gateway: '192.168.1.1', dns: '1.1.1.1, 8.8.8.8' });
assert.deepEqual(ok.errors, {});
assert.deepEqual(ok.cfg.dns, ['1.1.1.1', '8.8.8.8']);
assert.equal(V.cidr('10.42.0.1/24'), '');
assert.ok(V.cidr('10.42.0.1'));
assert.equal(V.psk('12345678'), '');
assert.ok(V.psk('1234567'));
assert.ok(V.ssid('x'.repeat(33)));
console.log('JS validation tests OK');
