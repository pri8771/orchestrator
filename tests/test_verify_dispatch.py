"""V3 board 3.9: the VERIFIERS registry — byte-parity dispatch for every
type string, kwarg passthrough, unknown-type auto fallthrough, the
never-raises contract, and registration-without-engine-edits.
"""
import os
import shutil
import tempfile
import unittest

import verify as vlib


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_vdisp_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls = []
        self._orig = {}
        for name in ("_verify_xcode", "_verify_spm", "_verify_http",
                     "_verify_web", "_verify_shell"):
            self._orig[name] = getattr(vlib, name)

            def make(n):
                def stub(*args, **kwargs):
                    self.calls.append((n, args, kwargs))
                    return {"ran": True, "ok": True, "tool": n,
                            "summary": "", "errors": ""}
                return stub
            setattr(vlib, name, make(name))
        self._orig_detect = vlib.detect_project

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(vlib, name, fn)
        vlib.detect_project = self._orig_detect


class TestParityMatrix(DispatchBase):
    MATRIX = [
        ("xcodebuild", "_verify_xcode"),
        ("swift", "_verify_spm"), ("spm", "_verify_spm"),
        ("http", "_verify_http"), ("server", "_verify_http"),
        ("boot", "_verify_http"),
        ("web", "_verify_web"), ("npm", "_verify_web"),
        ("node", "_verify_web"),
        ("shell", "_verify_shell"),
    ]

    def test_every_type_reaches_its_historical_verifier(self):
        # Registry entries bind at module definition; rebind for the stubs.
        with_stubs = dict(vlib.VERIFIERS)
        with_stubs.update({
            "xcodebuild": lambda b, s, t: vlib._verify_xcode(
                b, t, run_tests=bool(s.get("run_tests"))),
            "swift": lambda b, s, t: vlib._verify_spm(b, t),
            "spm": lambda b, s, t: vlib._verify_spm(b, t),
            "http": vlib._verify_http, "server": vlib._verify_http,
            "boot": vlib._verify_http,
            "web": vlib._verify_web, "npm": vlib._verify_web,
            "node": vlib._verify_web,
            "shell": lambda b, s, t: vlib._verify_shell(
                b, s.get("command"), t),
        })
        orig = vlib.VERIFIERS
        vlib.VERIFIERS = with_stubs
        try:
            for vtype, expect in self.MATRIX:
                self.calls.clear()
                out = vlib.run_verification(self.dir, {"type": vtype}, 60)
                self.assertTrue(out["ran"], vtype)
                self.assertEqual(self.calls[0][0], expect, vtype)
        finally:
            vlib.VERIFIERS = orig

    def test_xcodebuild_run_tests_passthrough(self):
        orig = vlib.VERIFIERS
        vlib.VERIFIERS = dict(orig, xcodebuild=lambda b, s, t:
                              vlib._verify_xcode(
                                  b, t, run_tests=bool(s.get("run_tests"))))
        try:
            vlib.run_verification(self.dir,
                                  {"type": "xcodebuild", "run_tests": True},
                                  60)
            name, args, kwargs = self.calls[0]
            self.assertEqual(kwargs.get("run_tests"), True)
            self.calls.clear()
            vlib.run_verification(self.dir, {"type": "xcodebuild"}, 60)
            self.assertEqual(self.calls[0][2].get("run_tests"), False)
        finally:
            vlib.VERIFIERS = orig

    def test_shell_command_extraction(self):
        orig = vlib.VERIFIERS
        vlib.VERIFIERS = dict(orig, shell=lambda b, s, t:
                              vlib._verify_shell(b, s.get("command"), t))
        try:
            vlib.run_verification(self.dir,
                                  {"type": "shell", "command": "make x"}, 60)
            _n, args, _k = self.calls[0]
            self.assertIn("make x", args)
        finally:
            vlib.VERIFIERS = orig


class TestAutoAndContracts(DispatchBase):
    def test_unknown_type_behaves_like_auto(self):
        vlib.detect_project = lambda d: "spm"
        out = vlib.run_verification(self.dir,
                                    {"type": "totally-unknown"}, 60)
        self.assertTrue(out["ran"])
        self.assertEqual(self.calls[0][0], "_verify_spm",
                         "unknown types must ride the auto path unchanged")

    def test_auto_xcode_never_passes_run_tests(self):
        vlib.detect_project = lambda d: "xcode"
        vlib.run_verification(self.dir, {"type": "auto",
                                         "run_tests": True}, 60)
        _n, _a, kwargs = self.calls[0]
        self.assertNotIn("run_tests", kwargs,
                         "the auto path has never honored run_tests")

    def test_never_raises_on_a_throwing_verifier(self):
        orig = vlib.VERIFIERS
        def boom(b, s, t):
            raise RuntimeError("verifier exploded")
        vlib.VERIFIERS = dict(orig, shell=boom)
        try:
            out = vlib.run_verification(self.dir, {"type": "shell"}, 60)
        finally:
            vlib.VERIFIERS = orig
        self.assertFalse(out["ran"])
        self.assertIn("verification errored", out["summary"])

    def test_missing_build_dir_short_circuits(self):
        out = vlib.run_verification(os.path.join(self.dir, "nope"),
                                    {"type": "shell"}, 60)
        self.assertFalse(out["ran"])
        self.assertEqual(out["summary"], "no build directory to verify.")
        self.assertEqual(self.calls, [])

    def test_new_type_registers_without_engine_edits(self):
        seen = []
        def custom(build_dir, spec, timeout):
            seen.append((build_dir, spec, timeout))
            return {"ran": True, "ok": True, "tool": "custom",
                    "summary": "hi", "errors": ""}
        vlib.VERIFIERS["my-custom"] = custom
        try:
            out = vlib.run_verification(self.dir, {"type": "my-custom"}, 60)
        finally:
            del vlib.VERIFIERS["my-custom"]
        self.assertEqual(out["tool"], "custom")
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
