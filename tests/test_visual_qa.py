"""Visual QA gate (visualqa.py): screenshot the built app in the simulator
and grade with a local vision model. Unit coverage for the pure parts —
verdict parsing, model resolution, bundle-id extraction, .app discovery, and
the gate's skip paths — with subprocess/HTTP faked so the suite never needs a
simulator or Ollama."""
import json
import os
import plistlib
import tempfile
import unittest
import unittest.mock

import visualqa as vqa


def _cget(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _emit(_msg):
    pass


class TestOkBadParsing(unittest.TestCase):
    def test_leading_token(self):
        self.assertEqual(vqa.parse_ok_bad("OK — a styled timer screen."), "OK")
        self.assertEqual(vqa.parse_ok_bad("BAD. The screen is blank."), "BAD")

    def test_case_and_prose(self):
        self.assertEqual(vqa.parse_ok_bad("I'd say ok, the list renders."), "OK")
        self.assertEqual(vqa.parse_ok_bad("Verdict: bad — home screen visible"),
                         "BAD")

    def test_no_token_returns_none(self):
        self.assertIsNone(vqa.parse_ok_bad("It looks quite nice overall."))
        self.assertIsNone(vqa.parse_ok_bad(""))
        # Word-boundary: OK inside another word must not match.
        self.assertIsNone(vqa.parse_ok_bad("The tokens are broken here"))


class TestCaptureGrantsPermissions(unittest.TestCase):
    """capture_screens pre-grants privacy permissions before screenshotting, so
    a first-launch permission dialog can't cover the app and cause a false BAD."""

    def test_grant_all_issued_before_any_screenshot(self):
        calls = []

        def fake_run(cmd, cwd=None, timeout=120):
            calls.append(cmd)
            # simctl launch prints a pid; screenshot writes the file.
            if "screenshot" in cmd:
                # create the target file so it's collected
                open(cmd[-1], "w").close()
            return 0, "", ""

        with unittest.mock.patch.object(vqa, "_install_with_rescue", return_value=""), \
             unittest.mock.patch.object(vqa, "_run", side_effect=fake_run):
            shots, note = vqa.capture_screens("UDID", "/tmp/App.app", "com.x.app",
                                              tempfile.mkdtemp())
        self.assertEqual(note, "")
        grant_idx = next((i for i, c in enumerate(calls)
                          if "privacy" in c and "grant" in c and "all" in c), None)
        self.assertIsNotNone(grant_idx, "no `simctl privacy grant all` call issued")
        first_shot = next((i for i, c in enumerate(calls) if "screenshot" in c), None)
        self.assertIsNotNone(first_shot)
        # The grant must happen BEFORE the first screenshot.
        self.assertLess(grant_idx, first_shot)
        # It targets the app's bundle id.
        self.assertIn("com.x.app", calls[grant_idx])


class TestGradeAggregation(unittest.TestCase):
    def _shots(self, names):
        d = tempfile.mkdtemp()
        paths = []
        for n in names:
            p = os.path.join(d, n)
            open(p, "wb").close()
            paths.append(p)
        return paths

    def test_all_ok_passes(self):
        real = vqa._ask_one
        vqa._ask_one = lambda m, b, mode, purpose, timeout=240: \
            ("OK", "styled %s screen" % mode)
        try:
            v = vqa.grade(["m1", "m2"],
                          self._shots(["main_light.png", "main_dark.png"]),
                          "", "a timer app")
        finally:
            vqa._ask_one = real
        self.assertEqual(v["verdict"], "PASS")
        self.assertEqual(v["score"], 100)
        self.assertEqual(v["issues"], [])

    def test_split_panel_vote_passes(self):
        """One grader says BAD, the other OK -> not unanimous -> screen OK."""
        real = vqa._ask_one
        vqa._ask_one = lambda m, b, mode, purpose, timeout=240: \
            (("BAD", "looks empty") if m == "m1" else ("OK", "styled screen"))
        try:
            v = vqa.grade(["m1", "m2"], self._shots(["main_light.png"]),
                          "", "a timer app")
        finally:
            vqa._ask_one = real
        self.assertEqual(v["verdict"], "PASS")

    def test_unanimous_bad_fails_with_issue(self):
        real = vqa._ask_one
        vqa._ask_one = lambda m, b, mode, purpose, timeout=240: \
            (("BAD", "unreadable dark text") if mode == "dark"
             else ("OK", "fine"))
        try:
            v = vqa.grade(["m1", "m2"],
                          self._shots(["main_light.png", "main_dark.png"]),
                          "", "a timer app")
        finally:
            vqa._ask_one = real
        self.assertEqual(v["verdict"], "FAIL")
        self.assertEqual(v["score"], 50)
        self.assertEqual(v["issues"][0]["screen"], "main_dark.png")
        self.assertIn("unreadable", v["issues"][0]["note"])

    def test_unusable_model_returns_none(self):
        real = vqa._ask_one
        vqa._ask_one = lambda m, b, mode, purpose, timeout=240: (None, "")
        try:
            v = vqa.grade("m", self._shots(["main_light.png"]), "", "app")
        finally:
            vqa._ask_one = real
        self.assertIsNone(v)


class TestModelResolution(unittest.TestCase):
    def test_auto_panel_from_installed(self):
        self.assertEqual(
            vqa.resolve_vision_models("", ["qwen2.5vl:3b", "gemma3:4b",
                                           "qwen2.5-coder:14b"]),
            ["gemma3:4b", "qwen2.5vl:3b"])

    def test_configured_override_grades_alone(self):
        self.assertEqual(
            vqa.resolve_vision_models("gemma3:12b", ["gemma3:12b", "gemma3:4b"]),
            ["gemma3:12b"])

    def test_nothing_installed_means_skip(self):
        self.assertEqual(vqa.resolve_vision_models("", ["qwen2.5-coder:14b"]), [])
        self.assertEqual(vqa.resolve_vision_models("gemma3:12b", []), [])


class TestBundleAndAppDiscovery(unittest.TestCase):
    def test_bundle_id_from_plist(self):
        app = tempfile.mkdtemp(suffix=".app")
        with open(os.path.join(app, "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.example.demo"}, fh)
        self.assertEqual(vqa.bundle_id(app), "com.example.demo")

    def test_bundle_id_missing_plist(self):
        self.assertEqual(vqa.bundle_id(tempfile.mkdtemp()), "")


class TestGateSkipPaths(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "app_build"), exist_ok=True)

    def test_disabled_skips(self):
        cfg = {"runtime": {"visual_qa_enabled": False}}
        self.assertIsNone(vqa.run_visual_qa(
            cfg, _cget, _emit, "x", self.app_dir, {}, "prompt"))

    def test_no_build_dir_skips(self):
        cfg = {"runtime": {"visual_qa_enabled": True}}
        self.assertIsNone(vqa.run_visual_qa(
            cfg, _cget, _emit, "x", tempfile.mkdtemp(), {}, "prompt"))

    def test_no_tools_skips(self):
        real = vqa.tools_available
        vqa.tools_available = lambda: False
        try:
            cfg = {"runtime": {"visual_qa_enabled": True}}
            self.assertIsNone(vqa.run_visual_qa(
                cfg, _cget, _emit, "x", self.app_dir, {}, "prompt"))
        finally:
            vqa.tools_available = real

    def test_ollama_down_skips(self):
        real_tools, real_up = vqa.tools_available, vqa.ollama_up
        vqa.tools_available = lambda: True
        vqa.ollama_up = lambda timeout=3: False
        try:
            cfg = {"runtime": {"visual_qa_enabled": True}}
            self.assertIsNone(vqa.run_visual_qa(
                cfg, _cget, _emit, "x", self.app_dir, {}, "prompt"))
        finally:
            vqa.tools_available, vqa.ollama_up = real_tools, real_up

    def test_fail_verdict_reaches_reason(self):
        """End-to-end through the gate with every effectful step faked."""
        real = (vqa.tools_available, vqa.ollama_up, vqa.resolve_vision_models,
                vqa.build_for_simulator, vqa.bundle_id, vqa.pick_simulator,
                vqa.capture_screens, vqa.grade)
        shots_taken = []

        def fake_capture(udid, app_path, bid, shots_dir):
            os.makedirs(shots_dir, exist_ok=True)
            p = os.path.join(shots_dir, "main_light.png")
            open(p, "wb").close()
            shots_taken.append(p)
            return [p], ""

        vqa.tools_available = lambda: True
        vqa.ollama_up = lambda timeout=3: True
        vqa.resolve_vision_models = lambda c, i: ["qwen2.5vl:3b"]
        vqa.build_for_simulator = lambda b, d, t: ("/tmp/Fake.app", "")
        vqa.bundle_id = lambda p: "com.example.fake"
        vqa.pick_simulator = lambda: ("UDID-1", "using booted iPhone 17")
        vqa.capture_screens = fake_capture
        vqa.grade = lambda m, i, d, p, timeout=240: {
            "score": 25, "verdict": "FAIL",
            "issues": [{"screen": "main_light", "severity": "high",
                        "note": "blank white screen"}]}
        try:
            cfg = {"runtime": {"visual_qa_enabled": True}}
            state = {"phase_outputs": {"design_handoff": "spec"}}
            reason = vqa.run_visual_qa(cfg, _cget, _emit, "x", self.app_dir,
                                       state, "prompt")
        finally:
            (vqa.tools_available, vqa.ollama_up, vqa.resolve_vision_models,
             vqa.build_for_simulator, vqa.bundle_id, vqa.pick_simulator,
             vqa.capture_screens, vqa.grade) = real
        self.assertIn("visual QA failed", reason)
        self.assertIn("blank white screen", reason)
        with open(os.path.join(self.app_dir, "docs", "visual_qa.json")) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["verdict"], "FAIL")
        self.assertEqual(saved["screenshots"],
                         [os.path.relpath(shots_taken[0], self.app_dir)])

    def test_pass_verdict_returns_none(self):
        real = (vqa.tools_available, vqa.ollama_up, vqa.resolve_vision_models,
                vqa.build_for_simulator, vqa.bundle_id, vqa.pick_simulator,
                vqa.capture_screens, vqa.grade)
        vqa.tools_available = lambda: True
        vqa.ollama_up = lambda timeout=3: True
        vqa.resolve_vision_models = lambda c, i: ["qwen2.5vl:3b"]
        vqa.build_for_simulator = lambda b, d, t: ("/tmp/Fake.app", "")
        vqa.bundle_id = lambda p: "com.example.fake"
        vqa.pick_simulator = lambda: ("UDID-1", "booted")

        def fake_capture(udid, app_path, bid, shots_dir):
            os.makedirs(shots_dir, exist_ok=True)
            p = os.path.join(shots_dir, "main_light.png")
            open(p, "wb").close()
            return [p], ""
        vqa.capture_screens = fake_capture
        vqa.grade = lambda m, i, d, p, timeout=240: {
            "score": 90, "verdict": "PASS", "issues": []}
        try:
            cfg = {"runtime": {"visual_qa_enabled": True}}
            self.assertIsNone(vqa.run_visual_qa(
                cfg, _cget, _emit, "x", self.app_dir,
                {"phase_outputs": {}}, "prompt"))
        finally:
            (vqa.tools_available, vqa.ollama_up, vqa.resolve_vision_models,
             vqa.build_for_simulator, vqa.bundle_id, vqa.pick_simulator,
             vqa.capture_screens, vqa.grade) = real


if __name__ == "__main__":
    unittest.main()
