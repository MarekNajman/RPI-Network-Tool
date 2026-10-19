import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from netpanel import auth as A
from netpanel import passwd


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp, clock, idle=300):
    path = os.path.join(tmp, "auth.json")
    with open(path, "w") as f:
        json.dump(A.hash_pin("482916", iterations=1000), f)
    return A.AuthManager(path, os.path.join(tmp, "state.json"), idle_timeout=idle, clock=clock), path


class HashTests(unittest.TestCase):
    def test_roundtrip(self):
        rec = A.hash_pin("482916", iterations=1000)
        self.assertTrue(A.verify_pin("482916", rec))
        self.assertFalse(A.verify_pin("482917", rec))
        self.assertFalse(A.verify_pin("482916", {"algo": "md5"}))
        self.assertNotIn("482916", json.dumps(rec))

    def test_policy(self):
        self.assertIsNone(A.pin_policy_error("482916"))
        for bad in ("12345", "1234567890123", "abc123", "111111", "123456", "987654", "890123"):
            self.assertIsNotNone(A.pin_policy_error(bad), bad)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.m, self.path = make(self.tmp.name, self.clock)

    def test_login_check_logout(self):
        tok = self.m.login("482916")
        self.assertTrue(self.m.check(tok))
        self.m.logout(tok)
        self.assertFalse(self.m.check(tok))
        self.assertFalse(self.m.check(None))
        self.assertFalse(self.m.check("forged"))

    def test_idle_expiry_and_sliding(self):
        tok = self.m.login("482916")
        self.clock.t += 290
        self.assertTrue(self.m.check(tok))          # activity refreshes the session
        self.clock.t += 300 + A.SERVER_GRACE_S - 1
        self.assertTrue(self.m.check(tok))
        self.clock.t += 300 + A.SERVER_GRACE_S + 1
        self.assertFalse(self.m.check(tok))

    def test_status_does_not_extend(self):
        tok = self.m.login("482916")
        self.clock.t += 200
        self.assertTrue(self.m.status(tok)["logged_in"])
        self.clock.t += 150
        # status() did not refresh the session: 350 s > 300 s idle + 45 s grace
        self.assertFalse(self.m.status(tok)["logged_in"])
        self.assertFalse(self.m.check(tok))

    def test_lockout_and_backoff(self):
        for i in range(A.MAX_FAILURES - 1):
            with self.assertRaises(A.AuthError) as cm:
                self.m.login("000000")
            self.assertEqual(cm.exception.attempts_left, A.MAX_FAILURES - 1 - i)
        with self.assertRaises(A.AuthError) as cm:
            self.m.login("000000")
        self.assertEqual(cm.exception.retry_after, A.LOCK_BASE_S)
        # even the right PIN is refused while locked
        with self.assertRaises(A.AuthError):
            self.m.login("482916")
        # lock survives a restart (persisted)
        m2 = A.AuthManager(self.path, os.path.join(self.tmp.name, "state.json"), clock=self.clock)
        with self.assertRaises(A.AuthError) as cm:
            m2.login("482916")
        self.assertGreater(cm.exception.retry_after, 0)
        # next round doubles the lock
        self.clock.t += A.LOCK_BASE_S + 1
        for _ in range(A.MAX_FAILURES - 1):
            with self.assertRaises(A.AuthError):
                self.m.login("000000")
        with self.assertRaises(A.AuthError) as cm:
            self.m.login("000000")
        self.assertEqual(cm.exception.retry_after, 2 * A.LOCK_BASE_S)
        # success resets everything
        self.clock.t += 2 * A.LOCK_BASE_S + 1
        self.assertTrue(self.m.check(self.m.login("482916")))
        self.assertEqual(self.m.status(None)["locked_for"], 0)

    def test_pin_change_closes_sessions(self):
        tok = self.m.login("482916")
        with open(self.path, "w") as f:
            json.dump(A.hash_pin("735102", iterations=1000), f, indent=4)  # different size -> new signature
        self.assertFalse(self.m.check(tok))
        self.assertTrue(self.m.check(self.m.login("735102")))

    def test_not_configured(self):
        m = A.AuthManager(os.path.join(self.tmp.name, "missing.json"), None, clock=self.clock)
        self.assertFalse(m.configured())
        with self.assertRaises(A.AuthError) as cm:
            m.login("482916")
        self.assertIn("netpanel-passwd", cm.exception.message)

    def test_session_cap(self):
        toks = [self.m.login("482916") for _ in range(A.MAX_SESSIONS + 2)]
        self.assertFalse(self.m.check(toks[0], touch=False))
        self.assertTrue(self.m.check(toks[-1]))


class PasswdTests(unittest.TestCase):
    def test_set_pin_writes_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "netpanel", "auth.json")
            answers = iter(["123456", "482916", "482916"])  # first one rejected by policy
            with mock.patch.object(passwd.getpass, "getpass", lambda prompt: next(answers)), \
                    mock.patch.object(passwd.os, "geteuid", lambda: 0), \
                    mock.patch.object(passwd.os, "chown", lambda *a: None), \
                    mock.patch.object(passwd.grp, "getgrnam", side_effect=KeyError):
                self.assertEqual(passwd.main(["--file", path]), 0)
            with open(path) as f:
                rec = json.load(f)
            self.assertTrue(A.verify_pin("482916", rec))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
