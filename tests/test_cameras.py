"""Camera search: Milesight protocol (framing, queries, reply parsing), ONVIF parsing,
merging, validation, a loopback test against a simulated camera and the HTTP API (demo)."""

import hashlib
import ipaddress
import json
import socket
import struct
import threading
import time
import unittest
import uuid

from netpanel.backend import cameras as C
from netpanel.backend.lanscan import LocalNet
from netpanel.models import Camera
from netpanel.validation import ValidationError, validate_cam_iface, validate_camera_ip

GUID = uuid.UUID("12345678-9abc-def0-1122-334455667788")

REPLY_IPC = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n<Reply>\r\n<Id>dca6324b7e01-ABC</Id>\r\n<Types>search</Types>\r\n'
    '<DeviceTypes>IPC</DeviceTypes>\r\n<MAC>1C:C3:16:2A:10:64</MAC>\r\n<IPv4Address>192.168.5.190</IPv4Address>\r\n'
    '<HttpPort>80</HttpPort>\r\n<HttpEnable>1</HttpEnable>\r\n<HttpsEnable>0</HttpsEnable>\r\n<HttpsPort>443</HttpsPort>\r\n'
    '<IPv4SubnetMask>255.255.255.0</IPv4SubnetMask>\r\n<IPv4Gateway>192.168.5.1</IPv4Gateway>\r\n<DNSIp>8.8.8.8</DNSIp>\r\n'
    '<Model>MS-C2964-RFLPE</Model>\r\n<Software>51.7.0.75</Software>\r\n<Hardware>V1.0</Hardware>\r\n'
    '<DeviceName>Gate &amp; yard</DeviceName>\r\n<ActiveStatus>0</ActiveStatus>\r\n<OemModel></OemModel>\r\n'
    '<Runtime>1234</Runtime>\r\n<SN>6418C2B0A190</SN>\r\n<PwdStrengthType>0</PwdStrengthType>\r\n</Reply>\r\n'
)

WSD_REPLY = b"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
  xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <SOAP-ENV:Header><wsa:RelatesTo>uuid:x</wsa:RelatesTo></SOAP-ENV:Header>
 <SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch>
  <wsa:EndpointReference><wsa:Address>urn:uuid:0aa1b2c3-0000-1111-2222-c056e3010271</wsa:Address></wsa:EndpointReference>
  <d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
  <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Yard%20camera
   onvif://www.onvif.org/hardware/DS-2CD2143G2-I onvif://www.onvif.org/location/city/hangzhou</d:Scopes>
  <d:XAddrs>http://192.168.10.71/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
  <d:MetadataVersion>10</d:MetadataVersion>
 </d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>"""


def c_checksum(data: bytes) -> int:
    """Literal transcription of Smart Tools FUN_007a0430 (decompiled), for cross-checking."""
    acc = 0
    i, n = 0, len(data)
    while n > 1:
        acc = (acc + struct.unpack_from("<H", data, i)[0]) & 0xFFFFFFFF
        i += 2
        n -= 2
    if n == 1:
        acc += data[i]
    v = (acc >> 16) + (acc & 0xFFFF)
    return (~((v >> 16) + v)) & 0xFFFF


class MilesightProtocolTests(unittest.TestCase):
    def test_checksum_matches_decompiled_routine(self):
        for sample in (b"", b"a", b"ab", b"abc", REPLY_IPC.encode(), bytes(range(256)) * 9, b"\xff" * 4097):
            self.assertEqual(C.ms_checksum(sample), c_checksum(sample), sample[:20])

    def test_checksum_property(self):
        # one's complement sum of data + checksum == 0xffff (even length)
        data = b"<Query>test</Query>\r\n!"
        cs = C.ms_checksum(data)
        self.assertEqual(C.ms_checksum(data + struct.pack("<H", cs)), 0)

    def test_frame_roundtrip(self):
        f = C.ms_frame(REPLY_IPC)
        body, ok = C.ms_unframe(f)
        self.assertTrue(ok)
        self.assertEqual(body, REPLY_IPC.encode())
        body, ok = C.ms_unframe(REPLY_IPC.encode())  # no trailer at all
        self.assertIsNone(ok)
        bad = f[:-2] + b"\x00\x00"
        body, ok = C.ms_unframe(bad)
        self.assertFalse(ok)

    def test_id_format(self):
        i = C.ms_id("DC:A6:32:4B:7E:01", GUID)
        self.assertEqual(i, "dca6324b7e01-12345678-9ABC-def0-1122-334455667788")
        self.assertTrue(C.ms_id("bogus", GUID).startswith("000000000000-"))

    def test_search_query_layout(self):
        q = C.ms_search_query("dc:a6:32:4b:7e:01", guid=GUID)
        body, ok = C.ms_unframe(q)
        self.assertTrue(ok)
        self.assertEqual(body.decode(), '<?xml version="1.0" encoding="UTF-8"?>\r\n<Query>\r\n'
                         '<Id>dca6324b7e01-12345678-9ABC-def0-1122-334455667788</Id>\r\n'
                         '<Types>search</Types>\r\n</Query>\r\n')
        self.assertLess(len(q), 256)  # Smart Tools uses a 256-byte buffer
        nvr = C.ms_unframe(C.ms_search_query("dc:a6:32:4b:7e:01", nvr=True, guid=GUID))[0].decode()
        self.assertIn("<DeviceTypes>NVR</DeviceTypes>\r\n</Query>", nvr)

    def test_modify_query(self):
        q = C.ms_modify_query("dc:a6:32:4b:7e:01", "1C-C3-16-2A-10-64", "192.168.10.80", "255.255.255.0",
                              "192.168.10.1", "8.8.8.8", 80, "Gate <1>", "admin", "ms123456", guid=GUID)
        body, ok = C.ms_unframe(q)
        self.assertTrue(ok)
        p = C.parse_ms(q)
        self.assertEqual(p["root"], "Query")
        f = p["fields"]
        self.assertEqual(f["Types"], "modify")
        self.assertEqual(f["MAC"], "1c:c3:16:2a:10:64")
        self.assertEqual(f["IPv4Address"], "192.168.10.80")
        self.assertEqual(f["HttpPort"], "80")
        self.assertEqual(f["DeviceName"], "Gate <1>")          # escaped on the wire, round-trips
        self.assertIn(b"Gate &lt;1&gt;", body)
        self.assertEqual(f["Password"], hashlib.md5(b"ms123456").hexdigest())
        self.assertNotIn(b"ms123456", q)                       # never the plain password
        order = [line.split(">")[0].lstrip("<") for line in body.decode().split("\r\n")[2:-2]]
        self.assertEqual(order, ["Id", "Types", "MAC", "IPv4Address", "HttpPort", "IPv4SubnetMask", "IPv4Gateway",
                                 "DNSIp", "DeviceName", "UserName", "Password"])

    def test_parse_reply_and_camera(self):
        p = C.parse_ms(C.ms_frame(REPLY_IPC))
        self.assertEqual(p["root"], "Reply")
        self.assertTrue(p["checksum"])
        cam = C.camera_from_ms(p["fields"], "eth0")
        self.assertEqual(cam.ip, "192.168.5.190")
        self.assertEqual(cam.mac, "1c:c3:16:2a:10:64")
        self.assertEqual(cam.kind, "IPC")
        self.assertEqual(cam.model, "MS-C2964-RFLPE")
        self.assertEqual(cam.name, "Gate & yard")
        self.assertEqual(cam.firmware, "51.7.0.75")
        self.assertEqual(cam.serial, "6418C2B0A190")
        self.assertEqual((cam.http_port, cam.https_port), (80, 443))
        self.assertEqual((cam.http_enabled, cam.https_enabled), (True, False))
        self.assertIs(cam.activated, False)
        self.assertTrue(cam.can_modify)
        self.assertEqual(cam.sources, ["milesight"])

    def test_active_status_semantics(self):
        base = {"IPv4Address": "10.0.0.5"}
        self.assertIs(C.camera_from_ms(dict(base), "").activated, True)       # missing -> active (Smart Tools)
        self.assertIs(C.camera_from_ms(dict(base, ActiveStatus=""), "").activated, True)
        self.assertIs(C.camera_from_ms(dict(base, ActiveStatus="1"), "").activated, True)
        self.assertIs(C.camera_from_ms(dict(base, ActiveStatus="0"), "").activated, False)
        self.assertEqual(C.camera_from_ms(dict(base, DeviceTypes="nvr"), "").kind, "NVR")
        self.assertIsNone(C.camera_from_ms({"IPv4Address": "nope"}, ""))

    def test_sloppy_xml_fallback(self):
        raw = REPLY_IPC.replace("Gate &amp; yard", "Gate & yard")  # unescaped & -> ET fails
        p = C.parse_ms(C.ms_frame(raw))
        self.assertEqual(p["root"], "Reply")
        self.assertEqual(p["fields"]["DeviceName"], "Gate & yard")
        self.assertEqual(p["fields"]["IPv4Address"], "192.168.5.190")

    def test_rejects_entities_and_garbage(self):
        evil = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><Reply><Types>&a;</Types></Reply>'
        self.assertIsNone(C.parse_ms(evil.encode()))
        self.assertIsNone(C.parse_ms(b""))
        self.assertIsNone(C.parse_ms(b"\x00\x01\x02garbage"))
        self.assertIsNone(C.parse_ms(b"<" + b"x" * (C.MAX_XML + 1)))


class OnvifTests(unittest.TestCase):
    def test_probe_is_valid_xml(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(C.wsd_probe("dn:NetworkVideoTransmitter", "uuid:1"))
        self.assertTrue(root.tag.endswith("Envelope"))
        self.assertIn(b"<d:Types>tds:Device</d:Types>", C.wsd_probe("tds:Device"))

    def test_parse_probe_match(self):
        recs = C.parse_wsd(WSD_REPLY)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertTrue(r["address"].startswith("urn:uuid:"))
        self.assertEqual(r["xaddrs"][0], "http://192.168.10.71/onvif/device_service")
        sc = C.scope_values(r["scopes"])
        self.assertEqual(sc["name"], "Yard camera")
        self.assertEqual(sc["hardware"], "DS-2CD2143G2-I")
        cam = C.camera_from_wsd(r, "192.168.10.71", "eth0")
        self.assertEqual((cam.ip, cam.name, cam.model, cam.http_port), ("192.168.10.71", "Yard camera", "DS-2CD2143G2-I", 80))
        self.assertEqual(cam.kind, "ONVIF")
        self.assertFalse(cam.can_modify)
        self.assertEqual(C.parse_wsd(b"not xml"), [])
        self.assertEqual(C.parse_wsd(b"<!DOCTYPE x><x/>"), [])

    def test_xaddr_port(self):
        rec = {"xaddrs": ["http://10.1.2.3:8080/onvif/device_service"], "scopes": [], "types": ""}
        cam = C.camera_from_wsd(rec, "10.1.2.3")
        self.assertEqual(cam.http_port, 8080)


class MergeTests(unittest.TestCase):
    def test_milesight_wins_onvif_fills(self):
        cams = {}
        C.merge(cams, Camera(ip="10.0.0.5", kind="ONVIF", name="onvif-name", onvif_url="http://10.0.0.5/onvif",
                             sources=["onvif"], http_port=80))
        C.merge(cams, Camera(ip="10.0.0.5", kind="IPC", name="Milesight name", model="MS-C1", sources=["milesight"],
                             can_modify=True, mac="1c:c3:16:00:00:01"))
        c = cams["10.0.0.5"]
        self.assertEqual(c.kind, "IPC")
        self.assertEqual(c.name, "Milesight name")
        self.assertEqual(c.onvif_url, "http://10.0.0.5/onvif")
        self.assertEqual(c.sources, ["milesight", "onvif"])
        self.assertTrue(c.can_modify)
        self.assertEqual(c.http_port, 80)


class ValidationTests(unittest.TestCase):
    GOOD = {"mac": "1c:c3:16:2a:10:64", "address": "192.168.10.80", "mask": "255.255.255.0",
            "gateway": "192.168.10.1", "dns": "", "http_port": "80", "username": "admin", "password": "x"}

    def test_ok(self):
        cfg = validate_camera_ip(dict(self.GOOD))
        self.assertEqual(cfg["netmask"], "255.255.255.0")
        self.assertEqual(cfg["prefix"], 24)
        self.assertEqual(cfg["http_port"], 80)
        self.assertEqual(cfg["dns"], "")
        cfg = validate_camera_ip(dict(self.GOOD, mask="/16", gateway="192.168.0.1", dns="1.1.1.1", http_port=8080))
        self.assertEqual((cfg["netmask"], cfg["dns"], cfg["http_port"]), ("255.255.0.0", "1.1.1.1", 8080))

    def test_errors(self):
        cases = [
            ({"mac": "zz"}, "mac"),
            ({"address": "192.168.10.0"}, "address"),
            ({"address": "300.1.1.1"}, "address"),
            ({"mask": "255.0.255.0"}, "mask"),
            ({"gateway": ""}, "gateway"),
            ({"gateway": "10.0.0.1"}, "gateway"),
            ({"dns": "nope"}, "dns"),
            ({"http_port": "0"}, "http_port"),
            ({"http_port": "70000"}, "http_port"),
            ({"username": ""}, "username"),
            ({"password": ""}, "password"),
            ({"password": "a\nb"}, "password"),
        ]
        for patch, field in cases:
            with self.assertRaises(ValidationError, msg=patch) as cm:
                validate_camera_ip(dict(self.GOOD, **patch))
            self.assertIn(field, cm.exception.errors, patch)

    def test_iface(self):
        nets = [LocalNet("eth0", "192.168.10.57", ipaddress.IPv4Network("192.168.10.0/24"), "dc:a6:32:4b:7e:01")]
        self.assertEqual(validate_cam_iface("eth0", nets).address, "192.168.10.57")
        with self.assertRaises(ValidationError):
            validate_cam_iface("wlan0", nets)


# --------------------------------------------------------------------------- loopback test with a simulated camera


def _local_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.123", 9))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class FakeMilesightCamera(threading.Thread):
    """Answers Milesight search/modify queries on 239.255.255.250:26360 like a camera would."""

    def __init__(self, local_ip, password="ms123456", verify_checksum=True):
        super().__init__(daemon=True)
        self.local_ip = local_ip
        self.password_md5 = hashlib.md5(password.encode()).hexdigest()
        self.verify_checksum = verify_checksum
        self.ip = "192.168.5.190"
        self.mac = "1c:c3:16:3b:51:90"
        self.queries = []
        self.stop_evt = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.bind(("", C.MS_PORT))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(C.MS_GROUP) + socket.inet_aton(local_ip))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self.sock.settimeout(0.1)

    def reply(self, items):
        self.sock.sendto(C.ms_frame(C.ms_xml("Reply", items)), (C.MS_GROUP, C.MS_PORT))

    def run(self):
        while not self.stop_evt.is_set():
            try:
                data, _src = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            msg = C.parse_ms(data)
            if not msg or msg["root"] != "Query":
                continue
            if self.verify_checksum and msg["checksum"] is not True:
                continue
            f = msg["fields"]
            self.queries.append(f)
            head = [("Id", f.get("Id", "")), ("Types", f.get("Types", ""))]
            if f.get("Types") == "search" and f.get("DeviceTypes") != "NVR":
                self.reply(head + [("DeviceTypes", "IPC"), ("MAC", self.mac.upper()), ("IPv4Address", self.ip),
                                   ("HttpPort", 80), ("IPv4SubnetMask", "255.255.255.0"), ("IPv4Gateway", "192.168.5.1"),
                                   ("DNSIp", "8.8.8.8"), ("Model", "MS-C2964-RFLPE"), ("Software", "51.7.0.75"),
                                   ("DeviceName", "Test camera"), ("ActiveStatus", "1")])
            elif f.get("Types") == "modify" and f.get("MAC") == self.mac:
                ok = f.get("UserName") == "admin" and f.get("Password") == self.password_md5
                if ok:
                    self.ip = f["IPv4Address"]
                self.reply(head + [("MAC", self.mac), ("Result", "success" if ok else "failed")])

    def close(self):
        self.stop_evt.set()
        self.join(2)
        self.sock.close()


class _NoLan:
    def local_networks(self):
        return []

    def neighbours(self, iface):
        return {}

    def vendor(self, mac):
        return ""


@unittest.skipUnless(_local_ipv4(), "no IPv4 interface for a multicast loopback test")
class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.ip = _local_ipv4()
        self.local = LocalNet("test0", self.ip, ipaddress.IPv4Network("%s/24" % self.ip, strict=False), "02:fc:00:00:00:01")
        try:
            self.cam = FakeMilesightCamera(self.ip)
        except OSError as e:
            self.skipTest("cannot join multicast group: %s" % e)
        self.cam.start()
        self.finder = C.CameraFinder(_NoLan())

    def tearDown(self):
        self.cam.close()

    def test_search_finds_camera_in_other_subnet(self):
        mgr = C.CameraManager(self.finder, duration=1.5)
        mgr.start(self.local)
        mgr.wait(10)
        snap = mgr.snapshot()
        self.assertEqual(snap["state"], "done", snap)
        ips = {c["ip"]: c for c in snap["cams"]}
        self.assertIn("192.168.5.190", ips, snap)
        c = ips["192.168.5.190"]
        self.assertEqual(c["mac"], "1c:c3:16:3b:51:90")
        self.assertFalse(c["in_subnet"])
        self.assertTrue(c["can_modify"])
        # the camera saw both queries with our MAC in the Id
        types = {(q.get("Types"), q.get("DeviceTypes", "")) for q in self.cam.queries}
        self.assertIn(("search", ""), types)
        self.assertIn(("search", "NVR"), types)
        self.assertTrue(all(q["Id"].startswith("02fc00000001-") for q in self.cam.queries))

    def test_modify(self):
        mgr = C.CameraManager(self.finder, duration=1.0)
        mgr.start(self.local)
        mgr.wait(10)
        cfg = {"address": "192.0.2.80", "netmask": "255.255.255.0", "gateway": "192.0.2.1", "dns": "", "http_port": 80}
        with self.assertRaises(C.CameraError) as cm:
            mgr.modify("1c:c3:16:3b:51:90", self.local, cfg, "admin", "wrong")
        self.assertIn("refused", cm.exception.message)
        msg = mgr.modify("1C-C3-16-3B-51-90", self.local, cfg, "admin", "ms123456")
        self.assertIn("192.0.2.80", msg)
        self.assertEqual(self.cam.ip, "192.0.2.80")
        q = [q for q in self.cam.queries if q.get("Types") == "modify"][-1]
        self.assertEqual(q["DNSIp"], "8.8.8.8")       # empty DNS -> keep the camera's current one
        self.assertEqual(q["DeviceName"], "Test camera")
        with self.assertRaises(C.CameraError):
            mgr.modify("1c:c3:16:00:00:00", self.local, cfg, "admin", "ms123456")  # not in the last result

    def test_no_answer(self):
        self.cam.close()
        cam = Camera(ip="192.168.5.190", mac="1c:c3:16:3b:51:90", can_modify=True)
        cfg = {"address": "192.0.2.80", "netmask": "255.255.255.0", "gateway": "192.0.2.1", "dns": "", "http_port": 80}
        t0 = time.monotonic()
        with self.assertRaises(C.CameraError) as cm:
            self.finder.modify(self.local, cam, cfg, "admin", "x", wait=0.3, tries=2)
        self.assertIn("No answer", cm.exception.message)
        self.assertLess(time.monotonic() - t0, 3)


# --------------------------------------------------------------------------- HTTP API (demo backend)


class CamsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import urllib.request  # noqa: F401
        from netpanel.auth import AuthManager, hash_pin
        from netpanel.backend.demo import DemoBackend
        from netpanel.server import make_server
        auth = AuthManager(None, None, record=hash_pin("482916", iterations=1000))
        backend = DemoBackend(delay=0.0)
        backend.cams.engine.duration = 0.2
        cls.httpd = make_server(backend, host="127.0.0.1", port=0, auth=auth)
        cls.port = cls.httpd.server_address[1]
        cls.httpd.RequestHandlerClass.allowed_hosts = {"127.0.0.1:%d" % cls.port}
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = None
        code, data = cls.req(cls, "/api/auth/login", {"pin": "482916"})
        cls.token = data["token"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path, body=None, token=True):
        import urllib.error
        import urllib.request
        h = {}
        if token and self.token:
            h["X-NetPanel-Token"] = self.token
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

    def wait_done(self):
        for _ in range(50):
            code, data = self.req("/api/cams/status")
            if data["cams"]["state"] != "running":
                return data["cams"]
            time.sleep(0.1)
        self.fail("search did not finish")

    def test_requires_login(self):
        for path in ("/api/cams", "/api/cams/status"):
            self.assertEqual(self.req(path, token=False)[0], 401)
        self.assertEqual(self.req("/api/cams/start", {"iface": "eth0"}, token=False)[0], 401)
        self.assertEqual(self.req("/api/cams/ip", {}, token=False)[0], 401)

    def test_search_and_change_ip(self):
        code, data = self.req("/api/cams")
        self.assertEqual(code, 200)
        self.assertEqual(data["suggested"], "eth0")
        self.assertEqual({n["iface"] for n in data["networks"]}, {"eth0", "wlan0"})
        code, data = self.req("/api/cams/start", {"iface": "nope0"})
        self.assertEqual(code, 400)
        code, data = self.req("/api/cams/start", {"iface": "eth0"})
        self.assertEqual(code, 200, data)
        snap = self.wait_done()
        self.assertEqual(snap["state"], "done")
        by_ip = {c["ip"]: c for c in snap["cams"]}
        self.assertIn("192.168.5.190", by_ip)
        other = by_ip["192.168.5.190"]
        self.assertFalse(other["in_subnet"])
        self.assertFalse(other["activated"])
        self.assertTrue(by_ip["192.168.10.64"]["in_subnet"])
        body = {"mac": other["mac"], "iface": "eth0", "address": "192.168.10.90", "mask": "255.255.255.0",
                "gateway": "192.168.10.1", "dns": "", "http_port": 80, "username": "admin", "password": "wrong"}
        code, data = self.req("/api/cams/ip", body)
        self.assertEqual(code, 502)
        self.assertIn("refused", data["message"])
        code, data = self.req("/api/cams/ip", dict(body, gateway="10.0.0.1"))
        self.assertEqual(code, 400)
        self.assertIn("gateway", data["errors"])
        code, data = self.req("/api/cams/ip", dict(body, password="secret"))
        self.assertEqual(code, 200, data)
        self.req("/api/cams/start", {"iface": "eth0"})
        snap = self.wait_done()
        moved = {c["mac"]: c for c in snap["cams"]}[other["mac"]]
        self.assertEqual(moved["ip"], "192.168.10.90")
        self.assertTrue(moved["in_subnet"])


if __name__ == "__main__":
    unittest.main()
