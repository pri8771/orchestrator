"""Real `xcodebuild test` execution (item 2): build-then-test ordering,
graceful degradation when no test target is discoverable, and the additive
tests_ran/tests_ok fields in persisted verify results."""
import json
import os
import tempfile
import unittest
import unittest.mock

import orchestrator as orch
import verify as verifylib

LIST_WITH_TESTS = """Information about project 'Demo':
    Targets:
        Demo
        DemoTests
        DemoUITests

    Build Configurations:
        Debug
        Release

    Schemes:
        Demo
"""

LIST_NO_TESTS = """Information about project 'Demo':
    Targets:
        Demo

    Build Configurations:
        Debug
        Release

    Schemes:
        Demo
"""


def _make_project(root):
    proj = os.path.join(root, "Demo.xcodeproj")
    os.makedirs(proj)
    return proj


class TestHasTestTarget(unittest.TestCase):
    def test_detects_test_target_in_targets_section(self):
        self.assertTrue(verifylib._has_test_target(LIST_WITH_TESTS, ["Demo"]))

    def test_false_when_no_test_target_or_scheme(self):
        self.assertFalse(verifylib._has_test_target(LIST_NO_TESTS, ["Demo"]))

    def test_detects_via_scheme_name(self):
        self.assertTrue(verifylib._has_test_target("no useful sections here",
                                                    ["DemoUITests"]))

    def test_empty_output_is_false(self):
        self.assertFalse(verifylib._has_test_target(None, []))
        self.assertFalse(verifylib._has_test_target("", []))


class TestVerifyXcodeRunTests(unittest.TestCase):
    def _stub_run(self, build_result=0, test_result=0, list_output=LIST_WITH_TESTS):
        calls = []

        def fake_run(cmd, cwd, timeout):
            calls.append(cmd)
            if "-list" in cmd:
                return (0, list_output, "") if list_output is not None else (1, "", "boom")
            if cmd[:3] == ["xcrun", "simctl", "list"]:
                # _concrete_sim_destination()'s device query: hand it a booted
                # iPhone so the test action gets a concrete destination.
                return (0, json.dumps({"devices": {
                    "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
                        {"udid": "TEST-UDID-1234", "state": "Booted",
                         "name": "iPhone 15", "isAvailable": True}]}}), "")
            if cmd[-1] == "test":
                # Both outcomes must carry the positive evidence
                # _tests_actually_executed() requires (a Test Suite banner /
                # Executed tally) — a bare failure string now reads as a
                # harness refusal, not a failing test run.
                if test_result == 0:
                    return (0, "Test Suite 'DemoTests' passed.\n"
                               "Executed 3 tests, with 0 failures", "")
                return (test_result,
                        "Test Suite 'DemoTests' failed.\n"
                        "Executed 3 tests, with 1 failure",
                        "XCTAssertEqual failed")
            if cmd[-1] == "build":
                return (build_result, "build output" if build_result == 0 else "", "" if build_result == 0 else "error: cannot find X")
            raise AssertionError("unexpected command: %r" % (cmd,))
        return calls, fake_run

    def test_run_tests_false_never_invokes_test_action(self):
        with tempfile.TemporaryDirectory() as d:
            _make_project(d)
            calls, fake_run = self._stub_run()
            with unittest.mock.patch.object(verifylib, "_run", fake_run), \
                 unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"):
                res = verifylib._verify_xcode(d, 30, run_tests=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["tests_ran"])
        self.assertIsNone(res["tests_ok"])
        self.assertFalse(any(c[-1] == "test" for c in calls))

    def test_run_tests_true_runs_after_successful_build(self):
        with tempfile.TemporaryDirectory() as d:
            _make_project(d)
            calls, fake_run = self._stub_run(build_result=0, test_result=0)
            with unittest.mock.patch.object(verifylib, "_run", fake_run), \
                 unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"):
                res = verifylib._verify_xcode(d, 30, run_tests=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["tests_ran"])
        self.assertTrue(res["tests_ok"])
        self.assertTrue(any(c[-1] == "build" for c in calls))
        self.assertTrue(any(c[-1] == "test" for c in calls))
        # build must run before test.
        actions = [c[-1] for c in calls if c[-1] in ("build", "test")]
        self.assertEqual(actions, ["build", "test"])
        # The test action must target the CONCRETE simulator resolved from
        # simctl, never the generic build destination (xcodebuild rejects it).
        test_cmd = next(c for c in calls if c[-1] == "test")
        self.assertIn("platform=iOS Simulator,id=TEST-UDID-1234", test_cmd)

    def test_failing_test_run_is_recorded_but_ok_reflects_build_only(self):
        with tempfile.TemporaryDirectory() as d:
            _make_project(d)
            _calls, fake_run = self._stub_run(build_result=0, test_result=1)
            with unittest.mock.patch.object(verifylib, "_run", fake_run), \
                 unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"):
                res = verifylib._verify_xcode(d, 30, run_tests=True)
        self.assertTrue(res["ok"])          # compile succeeded
        self.assertTrue(res["tests_ran"])
        self.assertFalse(res["tests_ok"])   # but tests failed
        self.assertIn("XCTAssertEqual failed", res["errors"])

    def test_test_action_skipped_when_build_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _make_project(d)
            calls, fake_run = self._stub_run(build_result=1)
            with unittest.mock.patch.object(verifylib, "_run", fake_run), \
                 unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"):
                res = verifylib._verify_xcode(d, 30, run_tests=True)
        self.assertFalse(res["ok"])
        self.assertFalse(res["tests_ran"])
        self.assertIsNone(res["tests_ok"])
        self.assertFalse(any(c[-1] == "test" for c in calls))
        self.assertIn("skipped", res["summary"])

    def test_no_test_target_degrades_gracefully_never_hard_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _make_project(d)
            calls, fake_run = self._stub_run(build_result=0, list_output=LIST_NO_TESTS)
            with unittest.mock.patch.object(verifylib, "_run", fake_run), \
                 unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"):
                res = verifylib._verify_xcode(d, 30, run_tests=True)
        self.assertTrue(res["ran"])
        self.assertTrue(res["ok"])          # build-only verification still succeeds
        self.assertFalse(res["tests_ran"])
        self.assertIsNone(res["tests_ok"])
        self.assertIn("no test target discoverable", res["summary"])
        self.assertFalse(any(c[-1] == "test" for c in calls))


class TestPersistedResultFields(unittest.TestCase):
    def test_tests_fields_additive_default(self):
        with tempfile.TemporaryDirectory() as d:
            rec = verifylib.persist_verify_result(
                d, "build_verification",
                {"ran": True, "ok": True, "tool": "xcodebuild"})
        self.assertIn("tests_ran", rec)
        self.assertIn("tests_ok", rec)
        self.assertFalse(rec["tests_ran"])
        self.assertIsNone(rec["tests_ok"])
        # Existing fields still present/unchanged.
        self.assertTrue(rec["ok"])
        self.assertTrue(rec["ran"])

    def test_tests_fields_carried_through_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(
                d, "build_verification",
                {"ran": True, "ok": True, "tool": "xcodebuild",
                 "tests_ran": True, "tests_ok": False})
            latest = verifylib.latest_verify_result(d)
        self.assertTrue(latest["tests_ran"])
        self.assertFalse(latest["tests_ok"])


class TestReleaseGateTestsOptIn(unittest.TestCase):
    """Test failures must never block release unless runtime.tests_gate_release
    is explicitly turned on (default False = purely observational)."""

    def _phases(self):
        import workflows as wf
        return [wf.Phase("build_verification", "x", "x.md", "p",
                         verify={"type": "xcodebuild"})]

    def test_default_off_ignores_failed_tests(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(
                d, "build_verification",
                {"ran": True, "ok": True, "tool": "xcodebuild",
                 "tests_ran": True, "tests_ok": False})
            reason = orch._release_gate_failure(d, self._phases(), {}, "build an app",
                                                cfg={"runtime": {}})
        self.assertIsNone(reason)

    def test_enabled_blocks_on_failed_tests(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(
                d, "build_verification",
                {"ran": True, "ok": True, "tool": "xcodebuild",
                 "tests_ran": True, "tests_ok": False})
            reason = orch._release_gate_failure(
                d, self._phases(), {}, "build an app",
                cfg={"runtime": {"tests_gate_release": True}})
        self.assertIsNotNone(reason)
        self.assertIn("test", reason.lower())

    def test_enabled_but_tests_never_ran_does_not_block(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(
                d, "build_verification",
                {"ran": True, "ok": True, "tool": "xcodebuild"})
            reason = orch._release_gate_failure(
                d, self._phases(), {}, "build an app",
                cfg={"runtime": {"tests_gate_release": True}})
        self.assertIsNone(reason)

    def test_failed_compile_still_blocks_regardless_of_tests_knob(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(
                d, "build_verification",
                {"ran": True, "ok": False, "tool": "xcodebuild",
                 "summary": "compile FAILED"})
            reason = orch._release_gate_failure(
                d, self._phases(), {}, "build an app", cfg={"runtime": {}})
        self.assertIsNotNone(reason)


class TestRunGeneratedTestsKillSwitch(unittest.TestCase):
    def test_spec_unchanged_when_run_tests_not_requested(self):
        spec = {"type": "xcodebuild"}
        self.assertIs(orch._gated_verify_spec({"runtime": {}}, spec), spec)

    def test_default_knob_leaves_run_tests_true(self):
        spec = {"type": "xcodebuild", "run_tests": True}
        out = orch._gated_verify_spec({"runtime": {}}, spec)
        self.assertTrue(out["run_tests"])

    def test_knob_off_forces_run_tests_false(self):
        spec = {"type": "xcodebuild", "run_tests": True}
        out = orch._gated_verify_spec({"runtime": {"run_generated_tests": False}}, spec)
        self.assertFalse(out["run_tests"])
        # Original spec dict is untouched.
        self.assertTrue(spec["run_tests"])


class TestNoRunnableAppIsFailure(unittest.TestCase):
    """An iOS-app build that produces no runnable app target (no
    .xcodeproj/.xcworkspace, none generatable) is a FAILURE, not "unverified"
    — the gap that let a Gloam rebuild ship an SPM library as done. "unverified"
    (ran=False) stays reserved for a genuinely absent toolchain."""

    def test_no_project_with_xcodebuild_present_is_failed(self):
        with tempfile.TemporaryDirectory() as d:
            with unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"), \
                 unittest.mock.patch.object(verifylib, "_generate_xcodeproj", return_value=""):
                res = verifylib._verify_xcode(d, 30)
        self.assertTrue(res["ran"])            # we COULD determine it
        self.assertFalse(res["ok"])            # ...and it did not build a runnable app
        self.assertEqual(verifylib.verification_status(res), "failed")

    def test_spm_library_only_is_called_out_as_not_a_runnable_app(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Package.swift"), "w") as fh:
                fh.write("// swift-tools-version:5.9\n")
            with unittest.mock.patch("shutil.which", return_value="/usr/bin/xcodebuild"), \
                 unittest.mock.patch.object(verifylib, "_generate_xcodeproj", return_value=""):
                res = verifylib._verify_xcode(d, 30)
        self.assertFalse(res["ok"])
        self.assertIn("SPM package", res["summary"])
        self.assertIn("not a runnable iOS app", res["summary"])

    def test_absent_xcodebuild_still_unverified_not_failed(self):
        # The one case that must STAY unverified: no toolchain to judge with.
        with tempfile.TemporaryDirectory() as d:
            with unittest.mock.patch("shutil.which", return_value=None):
                res = verifylib._verify_xcode(d, 30)
        self.assertFalse(res["ran"])
        self.assertEqual(verifylib.verification_status(res), "unverified")


if __name__ == "__main__":
    unittest.main()
