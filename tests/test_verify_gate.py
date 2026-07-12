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


class TestDetectStartInjection(unittest.TestCase):
    """A generated server file's name is interpolated into a `/bin/sh -lc`
    command; a crafted filename must not be able to inject shell."""

    def _write(self, d, name, body):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_malicious_fastapi_filename_is_skipped(self):
        d = tempfile.mkdtemp()
        # File name ends with "main.py" (so _find matches it) but embeds shell.
        self._write(d, "evil;touch pwned main.py", "app = FastAPI()\n")
        start, cwd, port = verify._detect_start(d, 8000)
        # mod "evil;touch pwned main" is not a Python identifier -> skipped.
        self.assertIsNone(start)
        self.assertFalse(os.path.exists(os.path.join(d, "pwned")))

    def test_normal_fastapi_filename_detected(self):
        d = tempfile.mkdtemp()
        self._write(d, "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        start, cwd, port = verify._detect_start(d, 8000)
        self.assertIsNotNone(start)
        self.assertIn("main:app", start)
        self.assertNotIn(";", start)

    def test_flask_filename_is_shell_quoted(self):
        d = tempfile.mkdtemp()
        self._write(d, "weird name app.py", "app = Flask(__name__)\n")
        start, cwd, port = verify._detect_start(d, 8000)
        self.assertIsNotNone(start)
        # The space-bearing filename must be quoted as a single shell token.
        self.assertIn(shlex_quote("weird name app.py"), start)


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


if __name__ == "__main__":
    unittest.main()
