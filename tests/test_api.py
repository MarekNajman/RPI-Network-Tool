"""HTTP API tests against the demo backend (no system changes)."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from netpanel.backend.demo import DemoBackend
from netpanel.server import make_server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server(DemoBackend(delay=0.0), host="127.0.0.1", port=0)
        cls.port = cls.httpd.server_address[1]
        # port 0 -> real port only known now; rebuild the Host allow-list
        cls.httpd.RequestHandlerClass.allowed_hosts = {"127.0.0.1:%d" % cls.port, "localhost:%d" % cls.port}
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path, body=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None
        h = {}
        if body is not None:
            data = json.dumps(body).encode()
            h = {"Content-Type": "application/json", "X-NetPanel": "1"}
        h.update(headers or {})
        r = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def test_status(self):
        code, data, _ = self.req("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(data["eth"]["iface"], "eth0")
        self.assertIn("tailscale", data)

    def test_static_files_and_csp(self):
        r = urllib.request.urlopen("http://127.0.0.1:%d/" % self.port, timeout=5)
        self.assertIn(b"NetPanel", r.read())
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])

    def test_path_traversal(self):
        r = urllib.request.Request("http://127.0.0.1:%d/../netpanel/server.py" % self.port)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 404)

    def test_post_requires_header(self):
        code, data, _ = self.req("/api/eth", {"method": "auto"}, headers={"X-NetPanel": "0"})
        self.assertEqual(code, 403)

    def test_foreign_host_rejected(self):
        code, _, _ = self.req("/api/status", headers={"Host": "evil.example:80"})
        self.assertEqual(code, 403)

    def test_eth_validation_error(self):
        code, data, _ = self.req("/api/eth", {"method": "manual", "address": "300.1.1.1", "prefix": 24})
        self.assertEqual(code, 400)
        self.assertIn("address", data["errors"])

    def test_eth_static_roundtrip(self):
        code, data, _ = self.req("/api/eth", {"method": "manual", "address": "192.168.10.60", "prefix": 24,
                                              "gateway": "192.168.10.1", "dns": ["1.1.1.1"]})
        self.assertEqual(code, 200, data)
        _, eth, _ = self.req("/api/eth")
        self.assertEqual(eth["eth"]["address"], "192.168.10.60")
        self.req("/api/eth", {"method": "auto"})

    def test_ap_overlap_rejected(self):
        code, data, _ = self.req("/api/wifi/ap", {"ssid": "x", "password": "12345678", "band": "bg", "channel": 6,
                                                  "address": "192.168.10.1", "prefix": 24})
        self.assertEqual(code, 400)
        self.assertIn("address", data["errors"])

    def test_tailscale_routes(self):
        code, data, _ = self.req("/api/tailscale/routes", {"network": "192.168.10.57", "prefix": 24})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["routes"], ["192.168.10.0/24"])
        self.assertEqual(data["command"], "sudo tailscale set --advertise-routes=192.168.10.0/24")
        code, data, _ = self.req("/api/tailscale/routes", {"routes": ["192.168.10.5/24"]})
        self.assertEqual(code, 400)
        code, data, _ = self.req("/api/tailscale/routes", {"clear": True})
        self.assertEqual(data["routes"], [])

    def test_suggested_route(self):
        _, data, _ = self.req("/api/tailscale")
        self.assertEqual(data["suggested"]["route"], "192.168.10.0/24")


if __name__ == "__main__":
    unittest.main()
