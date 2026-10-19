import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("display_kms", os.path.join(HERE, "..", "deploy", "display-kms.py"))
dk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dk)

BOOKWORM_DEFAULT = """# For more options and information see
# http://rptl.io/configtxt
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
# Enable DRM VC4 V3D driver
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1

[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[all]
"""


def active(text):
    return dk.analyse(text.splitlines(True))[0]


class DisplayKmsTests(unittest.TestCase):
    def test_default_adds_only_dsi(self):
        new, added, fkms, _ = dk.plan(BOOKWORM_DEFAULT)
        self.assertEqual(added, ["dtoverlay=vc4-kms-dsi-7inch\n"])
        self.assertTrue(new.rstrip().endswith("[all]\ndtoverlay=vc4-kms-dsi-7inch"))
        self.assertIn("vc4-kms-dsi-7inch", active(new))

    def test_commented_kms_is_added(self):
        cfg = BOOKWORM_DEFAULT.replace("dtoverlay=vc4-kms-v3d", "#dtoverlay=vc4-kms-v3d")
        _, added, _, _ = dk.plan(cfg)
        self.assertEqual(added, ["dtoverlay=vc4-kms-v3d\n", "dtoverlay=vc4-kms-dsi-7inch\n"])

    def test_overlay_under_foreign_filter_does_not_count(self):
        cfg = "[pi5]\ndtoverlay=vc4-kms-v3d\ndtoverlay=vc4-kms-dsi-7inch\n"
        new, added, _, _ = dk.plan(cfg)
        self.assertEqual(len(added), 2)
        self.assertEqual(active(new), {"vc4-kms-v3d", "vc4-kms-dsi-7inch"})

    def test_fkms_commented_out(self):
        cfg = "dtoverlay=vc4-fkms-v3d\nmax_framebuffers=2\n"
        new, added, fkms, _ = dk.plan(cfg)
        self.assertEqual(fkms, [0])
        self.assertTrue(new.startswith("#dtoverlay=vc4-fkms-v3d"))
        self.assertIn("dtoverlay=vc4-kms-v3d\n", added)

    def test_other_waveshare_overlay_respected(self):
        cfg = "dtoverlay=vc4-kms-v3d\ndtoverlay=vc4-kms-dsi-waveshare-panel,5_0_inch\n"
        new, added, _, _ = dk.plan(cfg)
        self.assertEqual(added, [])
        self.assertEqual(new, cfg)

    def test_idempotent(self):
        once = dk.plan(BOOKWORM_DEFAULT)[0]
        self.assertEqual(dk.plan(once)[0], once)

    def test_legacy_warning(self):
        _, _, _, legacy = dk.plan("lcd_rotate=2\n")
        self.assertEqual(legacy, ["lcd_rotate=2"])

    def test_apply_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.txt")
            with open(path, "w") as f:
                f.write(BOOKWORM_DEFAULT)
            orig_geteuid = dk.os.geteuid
            dk.os.geteuid = lambda: 0
            try:
                self.assertEqual(dk.main(["--apply", "--file=" + path]), 0)
                with open(path) as f:
                    self.assertIn("vc4-kms-dsi-7inch", f.read())
                self.assertTrue(os.path.exists(path + ".netpanel-bak"))
                self.assertEqual(dk.main(["--restore", "--file=" + path]), 0)
                with open(path) as f:
                    self.assertEqual(f.read(), BOOKWORM_DEFAULT)
            finally:
                dk.os.geteuid = orig_geteuid


if __name__ == "__main__":
    unittest.main()
