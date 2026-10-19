"""NetworkBackend tests with a fake nmcli: verifies parsing and exact commands."""

import subprocess
import unittest
from unittest import mock

from netpanel.backend import network as N
from netpanel.backend import nmparse as P
from netpanel.backend.runner import CommandError
from netpanel.models import APConfig, IPv4Config, StationRequest

ETH_SHOW = """GENERAL.DEVICE:eth0
GENERAL.TYPE:ethernet
GENERAL.HWADDR:DC:A6:32:01:02:03
GENERAL.STATE:100 (connected)
GENERAL.CONNECTION:Wired connection 1
IP4.ADDRESS[1]:192.168.10.57/24
IP4.GATEWAY:192.168.10.1
IP4.ROUTE[1]:dst = 192.168.10.0/24, nh = 0.0.0.0, mt = 100
IP4.DNS[1]:192.168.10.1
IP4.DNS[2]:1.1.1.1
"""

WIFI_LIST = """*:Office\\:WiFi:78:WPA2:36:5180 MHz
 :Guest:52::11:2462 MHz
 :Guest:60::1:2412 MHz
 ::40:WPA2:6:2437 MHz
 :Corp:40:WPA2 802.1X:1:2412 MHz
"""


class Fake:
    """Records nmcli invocations and answers from a small table."""

    def __init__(self, profiles=(), fail_on=None):
        self.calls = []
        self.profiles = set(profiles)
        self.fail_on = fail_on

    def __call__(self, args, timeout=20, check=True, secrets=(), quiet=False):
        args = list(args)
        self.calls.append(args)
        out = ""
        if self.fail_on and self.fail_on(args):
            raise CommandError(" ".join(args), 4, message="Connection activation failed: Secrets were required, but not provided")
        if args[-3:-1] == ["device", "show"]:
            out = ETH_SHOW if args[-1] == "eth0" else ETH_SHOW.replace("eth0", "wlan0")
        elif args[1:] == ["-t", "-f", "NAME", "connection", "show"]:
            out = "\n".join(self.profiles)
        elif "connection" in args and "show" in args and "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns" in args:
            out = "ipv4.method:auto\nipv4.addresses:\nipv4.gateway:--\nipv4.dns:\n"
        elif "connection.autoconnect" in args and "show" in args:
            out = "connection.autoconnect:yes\n"
        elif "show" in args and "netpanel-ap" in args and "-s" in args:
            out = ("802-11-wireless.ssid: My AP\n802-11-wireless.band:a\n802-11-wireless.channel:40\n"
                   "802-11-wireless-security.psk:pa:ss\\word \nipv4.addresses:10.42.0.1/24\n")
        elif "wifi" in args and "list" in args:
            out = WIFI_LIST
        if "add" in args:
            i = args.index("con-name")
            self.profiles.add(args[i + 1])
        if "delete" in args:
            self.profiles.discard(args[-1])
        return subprocess.CompletedProcess(args, 0, out, "")

    def find(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


class ParseTests(unittest.TestCase):
    def test_kv_and_escape(self):
        kv = P.parse_kv(ETH_SHOW)
        self.assertEqual(kv["GENERAL.HWADDR"], "DC:A6:32:01:02:03")
        self.assertEqual(P.collect(kv, "IP4.DNS"), ["192.168.10.1", "1.1.1.1"])
        self.assertEqual(P.state_name(kv["GENERAL.STATE"]), "connected")
        self.assertEqual(P.state_name("40 (connecting (prepare))"), "connecting")

    def test_values_are_verbatim(self):
        kv = P.parse_kv("802-11-wireless-security.psk:a:b\\c\n")
        self.assertEqual(kv["802-11-wireless-security.psk"], "a:b\\c")

    def test_tabular_escape(self):
        self.assertEqual(P.split_terse("*:Office\\:WiFi:78"), ["*", "Office:WiFi", "78"])

    def test_security(self):
        self.assertEqual(P.classify_security(""), "open")
        self.assertEqual(P.classify_security("WPA2"), "wpa-psk")
        self.assertEqual(P.classify_security("WPA2 WPA3"), "wpa-psk")
        self.assertEqual(P.classify_security("WPA3"), "sae")
        self.assertEqual(P.classify_security("WPA2 802.1X"), "enterprise")
        self.assertEqual(P.classify_security("WEP"), "wep")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.sysfs = mock.patch.object(N.NetworkBackend, "_sysfs", staticmethod(
            lambda iface, attr: {"carrier": "1", "speed": "1000", "duplex": "full"}.get(attr, "")))
        self.sysfs.start()
        self.addCleanup(self.sysfs.stop)

    def backend(self, fake):
        p = mock.patch.object(N, "run", fake)
        p.start()
        self.addCleanup(p.stop)
        return N.NetworkBackend()

    def test_eth_status(self):
        fake = Fake()
        b = self.backend(fake)
        st = b.eth_status()
        self.assertEqual((st.address, st.prefix, st.netmask), ("192.168.10.57", 24, "255.255.255.0"))
        self.assertEqual(st.mac, "dc:a6:32:01:02:03")
        self.assertEqual(st.method, "auto")
        self.assertEqual(st.speed, 1000)
        # multiline "show" output must be requested unescaped
        for c in fake.find("device", "show") + fake.find("connection", "show", "Wired connection 1"):
            self.assertEqual(c[c.index("-e") + 1], "no")

    def test_apply_static_creates_profile(self):
        fake = Fake()
        b = self.backend(fake)
        b.apply_eth(IPv4Config(method="manual", address="192.168.10.50", prefix=24, gateway="192.168.10.1", dns=["1.1.1.1", "8.8.8.8"]))
        add = fake.find("connection", "add")[0]
        self.assertIn("netpanel-eth", add)
        self.assertEqual(add[add.index("ipv4.addresses") + 1], "192.168.10.50/24")
        self.assertEqual(add[add.index("ipv4.dns") + 1], "1.1.1.1 8.8.8.8")
        up = fake.find("connection", "up", "netpanel-eth")[0]
        self.assertEqual(up[up.index("--wait") + 1], "60")

    def test_apply_dhcp_modifies_and_clears(self):
        fake = Fake(profiles={"netpanel-eth"})
        b = self.backend(fake)
        b.apply_eth(IPv4Config(method="auto"))
        mod = fake.find("connection", "modify", "netpanel-eth")[0]
        self.assertEqual(mod[mod.index("ipv4.method") + 1], "auto")
        self.assertEqual(mod[mod.index("ipv4.addresses") + 1], "")

    def test_scan_dedup_and_escape(self):
        b = self.backend(Fake())
        nets = b.wifi_scan()
        names = [n.ssid for n in nets]
        self.assertEqual(names[0], "Office:WiFi")  # in use first, colon unescaped
        self.assertEqual(names.count("Guest"), 1)
        self.assertEqual([n for n in nets if n.ssid == "Guest"][0].signal, 60)
        self.assertNotIn("", names)  # hidden SSIDs skipped

    def test_ap_config_keeps_special_chars(self):
        b = self.backend(Fake(profiles={"netpanel-ap"}))
        ap = b.ap_config()
        self.assertEqual(ap.password, "pa:ss\\word ")
        self.assertEqual(ap.ssid, " My AP")
        self.assertEqual((ap.band, ap.channel, ap.address, ap.prefix), ("a", 40, "10.42.0.1", 24))

    def test_start_ap_staged_and_promoted(self):
        fake = Fake(profiles={"netpanel-ap"})
        b = self.backend(fake)
        b.start_ap(APConfig(ssid="NP", password="supersecret", band="a", channel=36, address="10.42.0.1", prefix=24))
        add = fake.find("connection", "add", "netpanel-ap-new")[0]
        self.assertEqual(add[add.index("ipv4.method") + 1], "shared")
        self.assertEqual(add[add.index("802-11-wireless.mode") + 1], "ap")
        self.assertEqual(add[add.index("802-11-wireless-security.pmf") + 1], "disable")
        self.assertTrue(fake.find("connection", "up", "netpanel-ap-new"))
        # old profile replaced only after success, then the new one renamed
        self.assertTrue(fake.find("connection", "delete", "netpanel-ap"))
        self.assertEqual(fake.calls[-1][-4:], ["modify", "netpanel-ap-new", "connection.id", "netpanel-ap"])

    def test_start_ap_failure_keeps_old_profile(self):
        fake = Fake(profiles={"netpanel-ap", "netpanel-sta"}, fail_on=lambda a: a[-2:] == ["up", "netpanel-ap-new"])
        b = self.backend(fake)
        with self.assertRaises(CommandError) as cm:
            b.start_ap(APConfig(ssid="NP", password="supersecret", band="bg", channel=6, address="10.42.0.1", prefix=24))
        self.assertIn("restored", cm.exception.message)
        self.assertIn("netpanel-ap", fake.profiles)          # untouched
        self.assertNotIn("netpanel-ap-new", fake.profiles)   # cleaned up
        self.assertFalse([c for c in fake.calls if c[-2:] == ["delete", "netpanel-ap"]])

    def test_station_failure_rolls_back(self):
        # currently connected through the previous station profile
        fake = Fake(profiles={"netpanel-ap", "netpanel-sta"}, fail_on=lambda a: a[-2:] == ["up", "netpanel-sta-new"])
        b = self.backend(fake)
        with mock.patch.object(N.NetworkBackend, "_active_connection", lambda self, i: "netpanel-sta"):
            with self.assertRaises(CommandError) as cm:
                b.connect_station(StationRequest(ssid="Office", security="wpa-psk", password="wrongpass1"))
        self.assertIn("Wrong password", cm.exception.message)
        self.assertIn("netpanel-sta", fake.profiles)          # old credentials kept
        self.assertNotIn("netpanel-sta-new", fake.profiles)
        self.assertTrue([c for c in fake.calls if c[-4:] == ["modify", "netpanel-ap", "connection.autoconnect", "yes"]])
        self.assertTrue(fake.find("connection", "up", "netpanel-sta"))

    def test_station_success_promotes(self):
        fake = Fake(profiles={"netpanel-sta"})
        b = self.backend(fake)
        b.connect_station(StationRequest(ssid="Office", security="sae", password="supersecret"))
        add = fake.find("connection", "add", "netpanel-sta-new")[0]
        self.assertEqual(add[add.index("802-11-wireless-security.key-mgmt") + 1], "sae")
        self.assertEqual(fake.calls[-1][-4:], ["modify", "netpanel-sta-new", "connection.id", "netpanel-sta"])


if __name__ == "__main__":
    unittest.main()
