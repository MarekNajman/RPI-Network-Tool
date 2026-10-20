"""LAN scan: parsers, wire formats, vendor DB, discovery loop, job manager, API.

Nothing here sends packets: the discovery test replaces the UDP "poke" and the
`ip` command with scripted fakes.
"""

import ipaddress
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from netpanel.auth import AuthManager, hash_pin
from netpanel.backend import lanscan as ls
from netpanel.backend.demo import DemoBackend
from netpanel.backend.runner import CommandError
from netpanel.server import make_server
from netpanel.validation import ValidationError, validate_scan_range

ADDR_JSON = json.dumps([
    {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "link_type": "loopback",
     "address": "00:00:00:00:00:00",
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo"}]},
    {"ifindex": 2, "ifname": "eth0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "link_type": "ether",
     "address": "dc:a6:32:4b:7e:01",
     "addr_info": [{"family": "inet", "local": "192.168.10.57", "prefixlen": 24, "broadcast": "192.168.10.255",
                    "scope": "global", "dynamic": True, "noprefixroute": True, "label": "eth0"}]},
    {"ifindex": 3, "ifname": "wlan0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "link_type": "ether",
     "address": "dc:a6:32:4b:7e:02",
     "addr_info": [{"family": "inet", "local": "10.42.0.1", "prefixlen": 24, "scope": "global", "label": "wlan0"}]},
    {"ifindex": 4, "ifname": "tailscale0", "flags": ["POINTOPOINT", "NOARP", "UP", "LOWER_UP"], "link_type": "none",
     "addr_info": [{"family": "inet", "local": "100.101.102.103", "prefixlen": 32, "scope": "global"}]},
    {"ifindex": 5, "ifname": "docker0", "flags": ["BROADCAST", "MULTICAST", "UP"], "link_type": "ether",
     "address": "02:42:ac:11:00:01",
     "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16, "scope": "global"}]},
    {"ifindex": 6, "ifname": "eth1", "flags": ["BROADCAST", "MULTICAST"], "link_type": "ether",
     "address": "00:e0:4c:00:00:01",
     "addr_info": [{"family": "inet", "local": "10.9.9.9", "prefixlen": 24, "scope": "global"}]},
])


def neigh_json(entries):
    out = []
    for ip, state, mac in entries:
        e = {"dst": ip, "state": [state]}
        if mac:
            e["lladdr"] = mac
        out.append(e)
    return json.dumps(out)


class ParserTests(unittest.TestCase):
    def test_parse_addrs_keeps_only_up_ethernet_networks(self):
        nets = ls.parse_addrs(ADDR_JSON)
        self.assertEqual([(n.iface, str(n.network), n.address) for n in nets],
                         [("eth0", "192.168.10.0/24", "192.168.10.57"), ("wlan0", "10.42.0.0/24", "10.42.0.1")])
        self.assertEqual(nets[0].mac, "dc:a6:32:4b:7e:01")
        self.assertEqual(nets[0].to_json(), {"iface": "eth0", "address": "192.168.10.57", "network": "192.168.10.0",
                                             "prefix": 24, "cidr": "192.168.10.0/24"})

    def test_parse_neigh(self):
        t = ls.parse_neigh(neigh_json([("192.168.10.1", "REACHABLE", "4C:5E:0C:AA:10:01"),
                                       ("192.168.10.7", "FAILED", None)]) + "")
        self.assertEqual(t["192.168.10.1"].mac, "4c:5e:0c:aa:10:01")
        self.assertEqual(t["192.168.10.1"].state, {"REACHABLE"})
        self.assertEqual(t["192.168.10.7"].mac, "")
        self.assertEqual(ls.parse_neigh(""), {})

    def test_gateways(self):
        self.assertEqual(ls.parse_default_gateways(json.dumps([
            {"dst": "default", "gateway": "192.168.10.1", "dev": "eth0", "protocol": "dhcp", "metric": 100, "flags": []},
            {"dst": "default", "dev": "tailscale0"}])), {"192.168.10.1"})

    def test_targets_skip_network_and_broadcast_of_the_interface(self):
        local = ls.LocalNet("eth0", "192.168.10.57", ipaddress.ip_network("192.168.10.0/24"))
        t = ls.scan_targets(ipaddress.ip_network("192.168.10.0/24"), local)
        self.assertEqual(len(t), 254)
        self.assertEqual((t[0], t[-1]), ("192.168.10.1", "192.168.10.254"))
        # a sub-range keeps its own first/last address (they are normal hosts of the /24)
        self.assertEqual(ls.scan_targets(ipaddress.ip_network("192.168.10.64/30"), local),
                         ["192.168.10.64", "192.168.10.65", "192.168.10.66", "192.168.10.67"])
        self.assertEqual(ls.scan_targets(ipaddress.ip_network("192.168.10.5/32"), local), ["192.168.10.5"])

    def test_random_mac(self):
        self.assertTrue(ls.is_random_mac("da:7f:3e:9a:01:02"))
        self.assertTrue(ls.is_random_mac("6a:1e:2b:3c:4d:5e"))
        self.assertFalse(ls.is_random_mac("dc:a6:32:4b:7e:01"))
        self.assertFalse(ls.is_random_mac("garbage"))

    def test_dns_name_filter(self):
        self.assertTrue(ls.plausible_dns_name("nas01.office.lan", "192.168.10.12"))
        self.assertFalse(ls.plausible_dns_name("192-168-10-12.dyn.isp.net", "192.168.10.12"))
        self.assertFalse(ls.plausible_dns_name("12.10.168.192.in-addr.arpa", "192.168.10.12"))
        self.assertFalse(ls.plausible_dns_name("ip-192-168-10-12", "192.168.10.12"))
        self.assertTrue(ls.plausible_dns_name("host-112", "192.168.10.12"))
        self.assertFalse(ls.plausible_dns_name("192.168.10.12", "192.168.10.12"))

    def test_clean_name_and_vendor(self):
        self.assertEqual(ls.clean_name("Office-Printer.local."), "Office-Printer")
        self.assertEqual(ls.short_vendor("Raspberry Pi Trading Ltd"), "Raspberry Pi Trading")
        self.assertEqual(ls.short_vendor("Samsung Electronics Co.,Ltd"), "Samsung")
        self.assertEqual(ls.short_vendor("HUAWEI TECHNOLOGIES CO.,LTD"), "HUAWEI")
        self.assertEqual(ls.short_vendor("Siemens AG"), "Siemens")
        self.assertEqual(ls.short_vendor("Cisco Systems, Inc"), "Cisco Systems")
        self.assertEqual(ls.short_vendor("Intel Corporate"), "Intel Corporate")
        self.assertEqual(ls.short_vendor("Inc."), "Inc.")

    def test_leases(self):
        text = ("1729000000 aa:bb:cc:00:00:01 10.42.0.23 android-5f2 01:aa:bb:cc:00:00:01\n"
                "1729000000 aa:bb:cc:00:00:02 10.42.0.24 * *\n")
        self.assertEqual(ls.parse_leases(text), {"10.42.0.23": "android-5f2"})


class WireFormatTests(unittest.TestCase):
    def test_reverse_query(self):
        q = ls.dns_query(0x1234, ls.reverse_name("192.168.10.12"), 12)
        self.assertEqual(q[:12], struct.pack("!HHHHHH", 0x1234, 0, 1, 0, 0, 0))
        self.assertIn(b"\x0212\x0210\x03168\x03192\x07in-addr\x04arpa\x00", q)
        self.assertEqual(q[-4:], struct.pack("!HH", 12, 1))

    def ptr_response(self, target_labels, flags=0x8400):
        q = ls.dns_query(7, ls.reverse_name("192.168.10.12"), 12)
        hdr = struct.pack("!HHHHHH", 7, flags, 1, 1, 0, 0)
        rdata = b"".join(bytes([len(x)]) + x for x in target_labels) + b"\x00"
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 12, 0x8001, 120, len(rdata)) + rdata
        return hdr + q[12:] + answer

    def test_mdns_ptr_with_compression(self):
        self.assertEqual(ls.parse_ptr_response(self.ptr_response([b"nas01", b"local"])), "nas01.local")

    def test_ptr_rejects_queries_and_garbage(self):
        self.assertIsNone(ls.parse_ptr_response(self.ptr_response([b"x"], flags=0)))
        self.assertIsNone(ls.parse_ptr_response(b"\x00\x07\x84\x00\x00\x01\x00\x01\x00"))
        loop = struct.pack("!HHHHHH", 1, 0x8400, 1, 1, 0, 0) + b"\xc0\x0c"
        self.assertIsNone(ls.parse_ptr_response(loop))

    def test_nbstat(self):
        q = ls.nbstat_query(0x4242)
        self.assertEqual(len(q), 12 + 34 + 4)
        self.assertEqual(q[12:14], b"\x20C")

        def entry(name, suffix, flags):
            return name.ljust(15).encode() + bytes([suffix]) + struct.pack("!H", flags)

        names = entry("WORKGROUP", 0x00, 0x8400) + entry("WS-ENG-07", 0x00, 0x0400) + entry("WS-ENG-07", 0x20, 0x0400)
        rdata = bytes([3]) + names + bytes(6) + bytes(40)
        resp = (struct.pack("!HHHHHH", 0x4242, 0x8400, 0, 1, 0, 0) + ls.NBSTAT_NAME
                + struct.pack("!HHIH", 0x21, 1, 0, len(rdata)) + rdata)
        self.assertEqual(ls.parse_nbstat_response(resp), "WS-ENG-07")
        self.assertIsNone(ls.parse_nbstat_response(resp[:30]))


class OuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.mal = os.path.join(d, "oui.csv")
        self.mam = os.path.join(d, "mam.csv")
        with open(self.mal, "w") as f:
            f.write("Registry,Assignment,Organization Name,Organization Address\n"
                    'MA-L,DCA632,Raspberry Pi Trading Ltd,"Maurice Wilkes Building, Cambridge GB"\n'
                    "MA-L,70B3D5,IEEE Registration Authority,Piscataway US\n")
        with open(self.mam, "w") as f:
            f.write("Registry,Assignment,Organization Name,Organization Address\n"
                    "MA-M,70B3D5E,Example Sensors GmbH,Berlin DE\n")
        self.tab = os.path.join(d, "ieee-oui.txt")
        with open(self.tab, "w") as f:
            f.write("# arp-scan\nDCA632\tRaspberry Pi Trading Ltd\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_longest_prefix_wins(self):
        db = ls.OuiDB(csv_paths=[self.mal, self.mam, "/nonexistent.csv"], fallbacks=())
        self.assertEqual(db.lookup("dc:a6:32:4b:7e:01"), "Raspberry Pi Trading Ltd")
        self.assertEqual(db.lookup("70:b3:d5:e1:23:45"), "Example Sensors GmbH")
        self.assertEqual(db.lookup("70:b3:d5:01:23:45"), "IEEE Registration Authority")
        self.assertEqual(db.lookup("00:00:00:00:00:01"), "")
        self.assertTrue(db.available)

    def test_fallback_and_missing(self):
        db = ls.OuiDB(csv_paths=["/nonexistent.csv"], fallbacks=((self.tab, "tab"),))
        self.assertEqual(db.lookup("DC-A6-32-00-00-01"), "Raspberry Pi Trading Ltd")
        empty = ls.OuiDB(csv_paths=[], fallbacks=())
        self.assertEqual(empty.lookup("dc:a6:32:4b:7e:01"), "")
        self.assertFalse(empty.available)

    def test_wireshark_manuf_format(self):
        m = {}
        ls.OuiDB._fallback_line(m, "DC:A6:32\tRaspberr\tRaspberry Pi Trading Ltd", "manuf")
        ls.OuiDB._fallback_line(m, "70:B3:D5:E0:00:00/28\tExample\tExample Sensors GmbH", "manuf")
        self.assertEqual(m, {"DCA632": "Raspberry Pi Trading Ltd", "70B3D5E": "Example Sensors GmbH"})


class FakeRun:
    """Stands in for runner.run: scripted `ip -j ...` outputs."""

    def __init__(self, neigh_seq):
        self.neigh_seq = list(neigh_seq)
        self.calls = []

    def __call__(self, args, **_kw):
        self.calls.append(args)

        class R:
            stdout = ""
        if "neigh" in args:
            R.stdout = self.neigh_seq.pop(0) if len(self.neigh_seq) > 1 else self.neigh_seq[0]
        elif "addr" in args:
            R.stdout = ADDR_JSON
        elif "route" in args:
            R.stdout = json.dumps([{"dst": "default", "gateway": "192.168.10.1", "dev": "eth0"}])
        return R


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._poll = ls.POLL_S
        ls.POLL_S = 0.0

    def tearDown(self):
        ls.POLL_S = self._poll

    def scanner(self, seq):
        t = [0.0]

        def clock():
            t[0] += 0.5
            return t[0]
        s = ls.LanScanner(oui=ls.OuiDB(csv_paths=[], fallbacks=()), runner=FakeRun(seq), clock=clock)
        s.poked = []
        s.poke = lambda ips: s.poked.extend(ips)
        return s

    def test_states_decide_who_is_alive(self):
        ips = ["192.168.10.1", "192.168.10.2", "192.168.10.3", "192.168.10.4"]
        seq = [
            neigh_json([(ips[0], "REACHABLE", "aa:00:00:00:00:01"), (ips[1], "INCOMPLETE", None),
                        (ips[2], "STALE", "aa:00:00:00:00:03")]),
            neigh_json([(ips[0], "REACHABLE", "aa:00:00:00:00:01"), (ips[1], "REACHABLE", "aa:00:00:00:00:02"),
                        (ips[2], "DELAY", "aa:00:00:00:00:03"), (ips[3], "FAILED", None)]),
            neigh_json([(ips[0], "REACHABLE", "aa:00:00:00:00:01"), (ips[1], "REACHABLE", "aa:00:00:00:00:02"),
                        (ips[2], "PROBE", "aa:00:00:00:00:03"), (ips[3], "FAILED", None)]),
            # the device behind the stale entry is gone -> FAILED, not reported
            neigh_json([(ips[0], "REACHABLE", "aa:00:00:00:00:01"), (ips[1], "REACHABLE", "aa:00:00:00:00:02"),
                        (ips[2], "FAILED", "aa:00:00:00:00:03"), (ips[3], "FAILED", None)]),
        ]
        s = self.scanner(seq)
        found, progress = [], []
        s.discover("eth0", ips, threading.Event(), lambda ip, mac: found.append((ip, mac)), progress.append)
        self.assertEqual(s.poked, ips)
        self.assertEqual(found, [(ips[0], "aa:00:00:00:00:01"), (ips[1], "aa:00:00:00:00:02")])
        self.assertEqual(progress[-1], 4)
        self.assertTrue(all(a <= b for a, b in zip(progress, progress[1:])))

    def test_max_wait_bounds_a_stuck_entry(self):
        ips = ["192.168.10.9"]
        s = self.scanner([neigh_json([(ips[0], "PROBE", "aa:00:00:00:00:09")])])
        found = []
        s.discover("eth0", ips, threading.Event(), lambda ip, mac: found.append(ip), lambda n: None)
        self.assertEqual(found, [])
        neigh_calls = [c for c in s._run.calls if "neigh" in c]
        self.assertLessEqual(len(neigh_calls), int(ls.MAX_WAIT_S / 0.5) + 1)

    def test_batches_and_cancel(self):
        ips = ["10.0.%d.%d" % (i // 250, i % 250 + 1) for i in range(600)]
        s = self.scanner([neigh_json([])])
        cancel = threading.Event()
        calls = []

        def progress(n):
            calls.append(n)
            if n >= ls.BATCH:
                cancel.set()
        s.discover("eth0", ips, cancel, lambda *_: None, progress)
        self.assertEqual(len(s.poked), ls.BATCH)  # second batch never started
        self.assertEqual(s.poked, ips[:ls.BATCH])


class FakeEngine:
    def __init__(self, block=None, fail=False):
        self.block = block
        self.fail = fail
        self.vendors = {"4c:5e:0c:aa:10:01": "Routerboard.com", "dc:a6:32:4b:7e:01": "Raspberry Pi Trading Ltd"}

    def local_networks(self):
        return ls.parse_addrs(ADDR_JSON)

    def gateways(self):
        return {"192.168.10.1"}

    def hostname(self):
        return "panel"

    def prepare(self):
        pass

    def vendor(self, mac):
        return self.vendors.get(mac, "")

    def discover(self, iface, targets, cancel, on_found, on_progress):
        if self.fail:
            raise CommandError("ip -j -4 neigh show dev eth0", 1, message="Cannot find device \"eth0\"")
        on_found("192.168.10.20", "6a:00:00:00:00:20")
        on_found("192.168.10.1", "4c:5e:0c:aa:10:01")
        on_progress(len(targets) // 2)
        if self.block is not None:
            self.block.wait(5)
            if cancel.is_set():
                return
        on_progress(len(targets))

    def resolve_names(self, hosts, cancel):
        self.asked = sorted(ip for ip, _ in hosts)
        return {"192.168.10.1": {"dns": "gw.lan", "mdns": "router"}, "192.168.10.20": {}}


class ManagerTests(unittest.TestCase):
    def net(self):
        nets = ls.parse_addrs(ADDR_JSON)
        return validate_scan_range("192.168.10.0", "24", nets)

    def test_full_scan(self):
        eng = FakeEngine()
        m = ls.ScanManager(eng)
        snap = m.start(*self.net())
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["total"], 254)
        m.wait(5)
        s = m.snapshot()
        self.assertEqual(s["state"], "done")
        self.assertEqual([h["ip"] for h in s["hosts"]], ["192.168.10.1", "192.168.10.20", "192.168.10.57"])
        gw, phone, me = s["hosts"]
        self.assertEqual((gw["name"], gw["name_source"], gw["tags"], gw["vendor"]), ("router", "mdns", ["gateway"], "Routerboard.com"))
        self.assertEqual(gw["names"], {"dns": "gw.lan", "mdns": "router"})
        self.assertTrue(phone["random_mac"])
        self.assertEqual((phone["vendor"], phone["name"]), ("", ""))
        self.assertEqual((me["name"], me["tags"], me["vendor"]), ("panel", ["self"], "Raspberry Pi Trading"))
        self.assertEqual(eng.asked, ["192.168.10.1", "192.168.10.20"])  # own address is not resolved
        self.assertEqual(s["probed"], 254)
        self.assertIsNotNone(s["duration"])

    def test_busy_and_stop(self):
        gate = threading.Event()
        m = ls.ScanManager(FakeEngine(block=gate))
        m.start(*self.net())
        time.sleep(0.05)
        with self.assertRaises(ls.ScanBusy):
            m.start(*self.net())
        gate.set()  # let discover notice the cancel flag
        s = m.stop()
        self.assertEqual(s["state"], "stopped")
        self.assertEqual(s["count"], 3)
        # a new scan can start afterwards and replaces the results
        m.engine = FakeEngine()
        m.start(*self.net())
        m.wait(5)
        self.assertEqual(m.snapshot()["state"], "done")
        self.assertEqual(m.snapshot()["id"], 2)

    def test_error_is_reported(self):
        m = ls.ScanManager(FakeEngine(fail=True))
        m.start(*self.net())
        m.wait(5)
        s = m.snapshot()
        self.assertEqual(s["state"], "error")
        self.assertIn("Cannot find device", s["message"])

    def test_pick_name_order(self):
        self.assertEqual(ls.pick_name({"dns": "a", "netbios": "B", "mdns": "c", "dhcp": "d"}), ("d", "dhcp"))
        self.assertEqual(ls.pick_name({"dns": "a", "netbios": "B"}), ("B", "netbios"))
        self.assertEqual(ls.pick_name({}), ("", ""))


class RangeTests(unittest.TestCase):
    nets = ls.parse_addrs(ADDR_JSON)

    def test_valid_and_host_bits(self):
        net, local = validate_scan_range("192.168.10.57", "24", self.nets)
        self.assertEqual((str(net), local.iface), ("192.168.10.0/24", "eth0"))
        net, local = validate_scan_range("10.42.0.128", "/25", self.nets)
        self.assertEqual((str(net), local.iface), ("10.42.0.128/25", "wlan0"))

    def test_rejections(self):
        with self.assertRaises(ValidationError) as c:
            validate_scan_range("192.168.0.0", "16", self.nets)
        self.assertIn("maximum is /22", c.exception.errors["prefix"])
        with self.assertRaises(ValidationError) as c:
            validate_scan_range("172.16.5.0", "24", self.nets)
        self.assertIn("not inside a directly connected network", c.exception.message)
        self.assertIn("eth0 192.168.10.0/24", c.exception.message)
        with self.assertRaises(ValidationError) as c:
            validate_scan_range("192.168.10.0", "23", self.nets)  # bigger than the eth0 /24
        self.assertIn("network", c.exception.errors)
        with self.assertRaises(ValidationError) as c:
            validate_scan_range("192.168.10", "x", self.nets)
        self.assertEqual(set(c.exception.errors), {"network", "prefix"})

    def test_suggest(self):
        s = ls.suggest_range(ipaddress.ip_network("192.168.10.0/24"), "eth0", self.nets)
        self.assertEqual(s, {"network": "192.168.10.0", "prefix": 24, "source": "eth0", "note": ""})
        big = [ls.LocalNet("eth0", "10.1.7.20", ipaddress.ip_network("10.1.0.0/16"))]
        s = ls.suggest_range(ipaddress.ip_network("10.1.0.0/16"), "eth0", big)
        self.assertEqual((s["network"], s["prefix"]), ("10.1.7.0", 24))
        self.assertIn("/16", s["note"])
        s = ls.suggest_range(None, "eth0", self.nets[1:])
        self.assertEqual(s["source"], "wlan0")
        self.assertIsNone(ls.suggest_range(None, "eth0", []))


class ScanApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend = DemoBackend(delay=0.0)
        backend.scan.engine.duration = 0.3
        backend.scan.engine.names_delay = 0.1
        auth = AuthManager(None, None, record=hash_pin("482916", iterations=1000))
        cls.httpd = make_server(backend, host="127.0.0.1", port=0, auth=auth)
        cls.port = cls.httpd.server_address[1]
        cls.httpd.RequestHandlerClass.allowed_hosts = {"127.0.0.1:%d" % cls.port}
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = None
        cls.token = cls.req(cls, "/api/auth/login", {"pin": "482916"})[1]["token"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path, body=None, token=True):
        h = {"X-NetPanel-Token": self.token} if token and self.token else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.update({"Content-Type": "application/json", "X-NetPanel": "1"})
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, headers=h,
                                   method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_requires_login(self):
        for path in ("/api/scan", "/api/scan/status"):
            self.assertEqual(self.req(path, token=False)[0], 401)
        self.assertEqual(self.req("/api/scan/start", {"network": "192.168.10.0", "prefix": 24}, token=False)[0], 401)

    def test_flow(self):
        code, d = self.req("/api/scan")
        self.assertEqual(code, 200)
        self.assertEqual(d["suggested"]["network"], "192.168.10.0")
        self.assertEqual([n["iface"] for n in d["networks"]], ["eth0", "wlan0"])

        code, d = self.req("/api/scan/start", {"network": "172.16.0.0", "prefix": 24})
        self.assertEqual(code, 400)
        self.assertIn("network", d["errors"])

        code, d = self.req("/api/scan/start", {"network": "192.168.10.0", "prefix": "24"})
        self.assertEqual(code, 200, d)
        self.assertEqual(d["scan"]["state"], "running")
        code, d = self.req("/api/scan/start", {"network": "192.168.10.0", "prefix": "24"})
        self.assertEqual(code, 409)

        deadline = time.time() + 10
        while time.time() < deadline:
            d = self.req("/api/scan/status")[1]["scan"]
            if d["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(d["state"], "done")
        self.assertEqual(d["count"], 13)
        self.assertEqual(self.req("/api/scan/stop", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
