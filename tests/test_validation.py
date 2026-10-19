import unittest

from netpanel.models import APConfig, IPv4Config, StationRequest
from netpanel.validation import (
    ValidationError,
    canonical_route,
    netmask_from_prefix,
    parse_prefix,
    validate_ap,
    validate_ipv4_config,
    validate_psk,
    validate_routes,
    validate_ssid,
    validate_station,
    network_of,
)


class PrefixTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_prefix("24"), 24)
        self.assertEqual(parse_prefix("/16"), 16)
        self.assertEqual(parse_prefix("255.255.255.0"), 24)
        self.assertEqual(parse_prefix("255.255.254.0"), 23)
        self.assertEqual(parse_prefix(8), 8)

    def test_invalid(self):
        for bad in ("33", "abc", "255.0.255.0", "0.0.0.255", "", "24.5"):
            with self.assertRaises(ValueError, msg=bad):
                parse_prefix(bad)

    def test_netmask(self):
        self.assertEqual(netmask_from_prefix(24), "255.255.255.0")
        self.assertEqual(netmask_from_prefix(None), "")


class StaticTests(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(method="manual", address="192.168.1.50", prefix=24, gateway="192.168.1.1", dns=["1.1.1.1"])
        base.update(kw)
        return IPv4Config(**base)

    def test_ok(self):
        out = validate_ipv4_config(self.cfg())
        self.assertEqual(out.address, "192.168.1.50")
        self.assertEqual(out.prefix, 24)

    def test_dhcp_strips_values(self):
        out = validate_ipv4_config(IPv4Config(method="auto", address="garbage"))
        self.assertEqual(out.method, "auto")
        self.assertEqual(out.address, "")

    def test_errors(self):
        cases = {
            "address": self.cfg(address="192.168.1.256"),
            "gateway": self.cfg(gateway="10.0.0.1"),
            "dns": self.cfg(dns=["8.8.8"]),
        }
        for field, cfg in cases.items():
            with self.assertRaises(ValidationError) as cm:
                validate_ipv4_config(cfg)
            self.assertIn(field, cm.exception.errors)

    def test_network_and_broadcast_rejected(self):
        for addr in ("192.168.1.0", "192.168.1.255"):
            with self.assertRaises(ValidationError):
                validate_ipv4_config(self.cfg(address=addr, gateway=""))

    def test_leading_zero_rejected(self):
        with self.assertRaises(ValidationError):
            validate_ipv4_config(self.cfg(address="192.168.001.5"))

    def test_gateway_equals_address(self):
        with self.assertRaises(ValidationError):
            validate_ipv4_config(self.cfg(gateway="192.168.1.50"))

    def test_from_dict_dns_string(self):
        c = IPv4Config.from_dict({"method": "manual", "address": "10.0.0.5", "prefix": "8", "dns": "1.1.1.1, 9.9.9.9"})
        self.assertEqual(c.dns, ["1.1.1.1", "9.9.9.9"])
        self.assertEqual(c.prefix, 8)


class WifiTests(unittest.TestCase):
    def test_ssid(self):
        self.assertIsNone(validate_ssid("Office"))
        self.assertIsNotNone(validate_ssid(""))
        self.assertIsNotNone(validate_ssid("x" * 33))
        self.assertIsNotNone(validate_ssid("ž" * 17))  # 34 bytes

    def test_psk(self):
        self.assertIsNone(validate_psk("12345678"))
        self.assertIsNone(validate_psk("a" * 64 if False else "0123456789abcdef" * 4))
        self.assertIsNotNone(validate_psk("short"))
        self.assertIsNotNone(validate_psk("x" * 64))  # 64 chars must be hex
        self.assertIsNotNone(validate_psk("heslo-čeština"))

    def test_ap_ok(self):
        cfg = validate_ap(APConfig(ssid="NetPanel", password="supersecret", band="a", channel=40,
                                   address="10.42.0.1", prefix=24))
        self.assertEqual(cfg.channel, 40)

    def test_ap_errors(self):
        bad = APConfig(ssid="", password="x", band="a", channel=52, address="8.8.8.8", prefix=8)
        with self.assertRaises(ValidationError) as cm:
            validate_ap(bad)
        self.assertEqual(set(cm.exception.errors), {"ssid", "password", "channel", "address", "prefix"})

    def test_ap_overlap_with_eth(self):
        cfg = APConfig(ssid="A", password="12345678", band="bg", channel=6, address="192.168.1.1", prefix=24)
        with self.assertRaises(ValidationError) as cm:
            validate_ap(cfg, avoid_networks=[network_of("192.168.1.57", 24)])
        self.assertIn("overlaps", cm.exception.errors["address"])

    def test_ap_regdomain(self):
        base = dict(ssid="A", password="12345678", address="10.42.0.1", prefix=24)
        with self.assertRaises(ValidationError) as cm:
            validate_ap(APConfig(band="a", channel=36, **base), regdomain="00")
        self.assertIn("band", cm.exception.errors)
        with self.assertRaises(ValidationError):
            validate_ap(APConfig(band="bg", channel=13, **base), regdomain="US")
        self.assertEqual(validate_ap(APConfig(band="bg", channel=13, **base), regdomain="CZ").channel, 13)

    def test_station(self):
        ok = validate_station(StationRequest(ssid="Guest", security="open", password="ignored"))
        self.assertEqual(ok.password, "")
        with self.assertRaises(ValidationError):
            validate_station(StationRequest(ssid="X", security="wpa-eap"))
        with self.assertRaises(ValidationError):
            validate_station(StationRequest(ssid="X", security="wpa-psk", password="123"))


class RouteTests(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(canonical_route("192.168.10.57", 24), ("192.168.10.0/24", True))
        self.assertEqual(canonical_route("10.0.0.0", "8"), ("10.0.0.0/8", False))

    def test_rejects(self):
        for net, p in (("0.0.0.0", 0), ("127.0.0.0", 8), ("100.64.0.0", 10), ("100.100.1.0", 24), ("224.0.0.0", 4)):
            with self.assertRaises(ValidationError, msg=(net, p)):
                canonical_route(net, p)

    def test_validate_routes(self):
        self.assertEqual(validate_routes(["192.168.1.0/24", "192.168.1.0/24"]), ["192.168.1.0/24"])
        with self.assertRaises(ValidationError):
            validate_routes(["192.168.1.5/24"])
        with self.assertRaises(ValidationError):
            validate_routes(["192.168.1.0"])


if __name__ == "__main__":
    unittest.main()
