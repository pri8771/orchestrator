import os, tempfile, unittest
import verify


class TestVerifyGateLabel(unittest.TestCase):
    """The label the orchestrator derives is a pure function of persisted results."""
    def _label(self, app_dir, prompt_hash=None):
        latest = verify.latest_verify_result(app_dir, prompt_hash=prompt_hash)
        status = (latest.get("status") if latest else "unverified") or "unverified"
        return "VERIFICATION: %s" % status.upper()

    def test_verified(self):
        d = tempfile.mkdtemp()
        verify.persist_verify_result(d, "build", {"ran": True, "ok": True, "tool": "xcodebuild"}, prompt_hash="H")
        self.assertEqual(self._label(d, "H"), "VERIFICATION: VERIFIED")

    def test_failed(self):
        d = tempfile.mkdtemp()
        verify.persist_verify_result(d, "build", {"ran": True, "ok": False, "tool": "xcodebuild"}, prompt_hash="H")
        self.assertEqual(self._label(d, "H"), "VERIFICATION: FAILED")

    def test_unverified_when_none(self):
        d = tempfile.mkdtemp()
        self.assertEqual(self._label(d, "H"), "VERIFICATION: UNVERIFIED")

    def test_unverified_when_toolchain_absent(self):
        d = tempfile.mkdtemp()
        verify.persist_verify_result(d, "build", {"ran": False, "ok": False, "tool": "none"}, prompt_hash="H")
        self.assertEqual(self._label(d, "H"), "VERIFICATION: UNVERIFIED")


class TestSchemePicking(unittest.TestCase):
    """MarketingCampaignCockpit post-mortem: the first-listed scheme was a
    local Swift package (CockpitData), so verification blessed a package
    build while the app itself didn't compile."""

    def test_prefers_app_scheme_over_alphabetical_packages(self):
        schemes = ["CockpitData", "CockpitDomain", "CockpitServices",
                   "MarketingCampaignCockpit"]
        self.assertEqual(
            verify._pick_scheme(schemes, "/x/MarketingCampaignCockpit.xcodeproj"),
            "MarketingCampaignCockpit")

    def test_related_name_beats_first_listed(self):
        schemes = ["AppKitHelpers", "NickelApp"]
        self.assertEqual(verify._pick_scheme(schemes, "/x/Nickel.xcodeproj"),
                         "NickelApp")

    def test_falls_back_to_first_when_nothing_matches(self):
        self.assertEqual(verify._pick_scheme(["Alpha", "Beta"], "/x/Zed.xcodeproj"),
                         "Alpha")

    def test_empty_is_none(self):
        self.assertIsNone(verify._pick_scheme([], "/x/App.xcodeproj"))


if __name__ == "__main__":
    unittest.main()
