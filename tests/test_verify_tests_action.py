"""`xcodebuild test` destination + tests_ran honesty (streak 2026-07-21).

Two regressions from one live run: the test action was handed the same
`generic/platform=iOS Simulator` destination as the build action (which the
test action rejects before executing a single test), and that harness-level
refusal was then reported as tests_ran=True / "+ TESTS FAILED" — blaming the
app's tests for the harness's own misconfiguration.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import verify as vlib


def _fixture_project():
    """A build dir with a bare .xcodeproj marker so _verify_xcode proceeds."""
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "Streak.xcodeproj"))
    return d


_LIST_OUTPUT = "Targets:\n    Streak\n    StreakTests\n\nSchemes:\n    Streak\n"
_REFUSAL = ("xcodebuild: error: Failed to build project Streak with scheme "
            "Streak.: Cannot test target “StreakTests” on “Any "
            "iOS Simulator Device”: Tests must be run on a concrete "
            "device")
_SIMCTL = json.dumps({"devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
        {"udid": "AAAA-1111", "name": "iPhone Air", "state": "Shutdown"},
        {"udid": "BBBB-2222", "name": "iPhone 16 Pro Max", "state": "Booted"},
    ]}})


class _RunScript(object):
    """Scripted stand-in for verify._run keyed on the xcodebuild action."""

    def __init__(self, test_code=0, test_out="Test Suite 'All tests' passed.\n"
                                              "Executed 12 tests, with 0 failures"):
        self.calls = []
        self.test_code = test_code
        self.test_out = test_out

    def __call__(self, cmd, cwd, timeout):
        self.calls.append(cmd)
        if cmd[:2] == ["xcrun", "simctl"]:
            return 0, _SIMCTL, ""
        if "-list" in cmd:
            return 0, _LIST_OUTPUT, ""
        if "test" in cmd:
            return self.test_code, self.test_out, ""
        return 0, "BUILD SUCCEEDED", ""   # the build action


class TestConcreteDestinationForTests(unittest.TestCase):
    def test_test_action_gets_booted_concrete_destination_not_generic(self):
        script = _RunScript()
        with mock.patch.object(vlib, "_run", script), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            result = vlib._verify_xcode(_fixture_project(), 60, run_tests=True)
        test_cmds = [c for c in script.calls if "test" in c and "xcodebuild" in c[0]]
        self.assertEqual(len(test_cmds), 1)
        dest = test_cmds[0][test_cmds[0].index("-destination") + 1]
        self.assertEqual(dest, "platform=iOS Simulator,id=BBBB-2222")  # booted
        self.assertNotIn("generic", dest)
        self.assertTrue(result["tests_ran"])
        self.assertTrue(result["tests_ok"])
        self.assertIn("tests passed", result["summary"])

    def test_build_action_keeps_generic_destination(self):
        script = _RunScript()
        with mock.patch.object(vlib, "_run", script), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            vlib._verify_xcode(_fixture_project(), 60, run_tests=True)
        build_cmds = [c for c in script.calls
                      if c and c[0] == "xcodebuild" and "build" in c
                      and "test" not in c]
        self.assertTrue(build_cmds)
        dest = build_cmds[0][build_cmds[0].index("-destination") + 1]
        self.assertEqual(dest, "generic/platform=iOS Simulator")

    def test_booted_watch_or_tv_sim_never_wins_the_ios_destination(self):
        """A-37: simctl lists watchOS/tvOS runtimes too, and a paired watch
        sim commonly boots alongside an iPhone. A booted non-iOS UDID must
        never become the 'iOS Simulator' destination — the shutdown iPhone
        under an iOS runtime wins instead."""
        cross = json.dumps({"devices": {
            "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [
                {"udid": "WATCH-9999", "name": "Apple Watch Series 10",
                 "state": "Booted"}],
            "com.apple.CoreSimulator.SimRuntime.tvOS-18-0": [
                {"udid": "TV-8888", "name": "Apple TV 4K", "state": "Booted"}],
            "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
                {"udid": "AAAA-1111", "name": "iPhone 16",
                 "state": "Shutdown"}],
        }})
        with mock.patch.object(vlib, "_run",
                               lambda cmd, cwd, timeout: (0, cross, "")), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            dest = vlib._concrete_sim_destination()
        self.assertEqual(dest, "platform=iOS Simulator,id=AAAA-1111")

    def test_only_non_ios_sims_means_no_destination(self):
        """A-37: with only watch/TV simulators available there is no iOS
        destination at all — better to skip tests honestly than hand
        xcodebuild a watch UDID it will refuse."""
        watch_only = json.dumps({"devices": {
            "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [
                {"udid": "WATCH-9999", "name": "Apple Watch Series 10",
                 "state": "Booted"}],
        }})
        with mock.patch.object(vlib, "_run",
                               lambda cmd, cwd, timeout: (0, watch_only, "")), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            self.assertIsNone(vlib._concrete_sim_destination())

    def test_no_available_simulator_skips_tests_honestly(self):
        script = _RunScript()
        empty = json.dumps({"devices": {}})
        orig = script.__call__

        def no_sims(cmd, cwd, timeout):
            if cmd[:2] == ["xcrun", "simctl"]:
                return 0, empty, ""
            return orig(cmd, cwd, timeout)
        with mock.patch.object(vlib, "_run", no_sims), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            result = vlib._verify_xcode(_fixture_project(), 60, run_tests=True)
        self.assertFalse(result["tests_ran"])
        self.assertIsNone(result["tests_ok"])
        self.assertIn("tests could not run", result["summary"])
        self.assertNotIn("TESTS FAILED", result["summary"])


class TestHarnessRefusalIsNotATestFailure(unittest.TestCase):
    def test_refusal_without_executed_tests_reports_could_not_run(self):
        script = _RunScript(test_code=65, test_out=_REFUSAL)
        with mock.patch.object(vlib, "_run", script), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            result = vlib._verify_xcode(_fixture_project(), 60, run_tests=True)
        self.assertFalse(result["tests_ran"])
        self.assertIsNone(result["tests_ok"])
        self.assertIn("tests could not run", result["summary"])
        self.assertNotIn("TESTS FAILED", result["summary"])

    def test_real_test_failure_still_reports_tests_failed(self):
        """The honesty fix must not soften genuine failures: when XCTest
        really executed and failed, TESTS FAILED stays."""
        script = _RunScript(
            test_code=65,
            test_out="Test Suite 'StreakTests' started\n"
                     "Executed 12 tests, with 3 failures")
        with mock.patch.object(vlib, "_run", script), \
                mock.patch.object(vlib.shutil, "which", lambda n: "/usr/bin/x"):
            result = vlib._verify_xcode(_fixture_project(), 60, run_tests=True)
        self.assertTrue(result["tests_ran"])
        self.assertFalse(result["tests_ok"])
        self.assertIn("TESTS FAILED", result["summary"])


if __name__ == "__main__":
    unittest.main()
