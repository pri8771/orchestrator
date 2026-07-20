"""V3 7.4c: engine-side capability enforcement — writes/exec/external gated
on the running section's manifest, with flat/legacy sessions unrestricted.
"""
import os
import shutil
import tempfile
import unittest

import orchestrator as orch
import sections as seclib


def _cfg(root, app_dir):
    return {"root": root, "_app_dir": app_dir}


class TestCapabilityHelpers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _nested(self):
        d = os.path.join(self.root, "proj", "ideas", "chat-1")
        os.makedirs(d)
        return d

    def test_flat_legacy_session_is_unrestricted(self):
        # A 2-part / flat app path yields no section -> caps None ->
        # everything allowed (legacy runs must not be newly restricted).
        flat = os.path.join(self.root, "flatapp")
        os.makedirs(flat)
        cfg = _cfg(self.root, flat)
        self.assertIsNone(orch._section_capabilities(cfg))
        self.assertTrue(orch._section_exec_allowed(cfg))
        self.assertTrue(orch._section_external_allowed(cfg))

    def test_nested_section_defaults_deny_safe(self):
        # A nested session with no manifest defaults to workspace-only.
        cfg = _cfg(self.root, self._nested())
        # patch HERE so load_section reads our temp sections dir (none ->
        # built-in default, which is deny-safe for exec/external)
        saved = orch.HERE
        orch.HERE = self.root
        try:
            self.assertFalse(orch._section_exec_allowed(cfg))
            self.assertFalse(orch._section_external_allowed(cfg))
        finally:
            orch.HERE = saved

    def test_nested_section_honors_declared_escalation(self):
        cfg = _cfg(self.root, self._nested())
        secdir = os.path.join(self.root, "sections", "ideas")
        os.makedirs(secdir)
        import json
        with open(os.path.join(secdir, "section.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"id": "ideas", "title": "I", "workflow": "chat_ideas",
                       "capabilities": {"writes": "workspace", "exec": True,
                                        "external": True}}, fh)
        saved = orch.HERE
        orch.HERE = self.root
        try:
            self.assertTrue(orch._section_exec_allowed(cfg))
            self.assertTrue(orch._section_external_allowed(cfg))
        finally:
            orch.HERE = saved


class TestExecGate(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_exec_denied_returns_not_ran_without_calling_verify(self):
        import unittest.mock
        d = os.path.join(self.root, "proj", "ideas", "chat-1")
        os.makedirs(d)
        cfg = _cfg(self.root, d)
        quiet = orch._QUIET
        orch._QUIET = True
        saved = orch.HERE
        orch.HERE = self.root   # nested, no manifest -> exec False (default)
        try:
            with unittest.mock.patch.object(
                    orch.verifylib, "run_verification",
                    lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("exec-denied must not run verify"))):
                res = orch._gated_run_verification(cfg, "/build", {"type": "x"})
            self.assertFalse(res["ran"])
            self.assertEqual(res["status"], "unverified")
        finally:
            orch._QUIET, orch.HERE = quiet, saved

    def test_flat_session_runs_verify_normally(self):
        import unittest.mock
        flat = os.path.join(self.root, "flatapp")
        os.makedirs(flat)
        cfg = _cfg(self.root, flat)
        with unittest.mock.patch.object(
                orch.verifylib, "run_verification",
                lambda *a, **k: {"ran": True, "ok": True}):
            res = orch._gated_run_verification(cfg, "/build", {"type": "x"})
        self.assertTrue(res["ran"])


class TestExternalGate(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_api_preflight_refuses_when_section_denies_external(self):
        d = os.path.join(self.root, "proj", "ideas", "chat-1")
        os.makedirs(d)
        cfg = _cfg(self.root, d)
        saved = orch.HERE
        orch.HERE = self.root   # nested, default external False
        try:
            res = orch._api_preflight(cfg, "anthropic", "m", "cmd")
        finally:
            orch.HERE = saved
        self.assertIsNotNone(res)
        self.assertIn("denies external", res[1])
        self.assertEqual(res[2], 1)

    def test_api_preflight_flat_session_not_blocked_by_external_gate(self):
        # A flat app is unrestricted — the external gate must not fire (the
        # turn then proceeds to the normal opt-in/probe checks).
        flat = os.path.join(self.root, "flatapp")
        os.makedirs(flat)
        cfg = _cfg(self.root, flat)
        res = orch._api_preflight(cfg, "anthropic", "m", "cmd")
        # not blocked by external gate; blocked instead by opt-in (no api_agents)
        self.assertIsNotNone(res)
        self.assertNotIn("denies external", res[1])


if __name__ == "__main__":
    unittest.main()


class TestLibraryMiningBothGates(unittest.TestCase):
    """Review regression: the library_mining scaffold WRITES and the build
    EXECS — a {writes:none, exec:true} section must NOT scaffold (the write
    half of the bypass). Drives the REAL hook, not just the helpers."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app = "proj/ideas/chat-1"
        self.app_dir = os.path.join(self.root, self.app)
        os.makedirs(self.app_dir)
        self.quiet = orch._QUIET
        orch._QUIET = True

    def tearDown(self):
        orch._QUIET = self.quiet
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_hook(self, caps):
        import unittest.mock
        cfg = {"root": self.root, "_app_dir": self.app_dir,
               "_workflow_target": "library_mining", "_target_paths": []}
        # a valid extraction-json block so pkg is truthy
        block = ('```extraction-json\n{"package_name": "MyLib", '
                 '"public_api": [{"kind": "protocol", "name": "P", '
                 '"signature": "func f()"}]}\n```')
        scaffolded = {"called": False}

        def fake_scaffold(pkg, dest):
            scaffolded["called"] = True
            return {"ok": False}

        with unittest.mock.patch.object(orch, "_section_capabilities",
                                        lambda c: caps), \
                unittest.mock.patch.object(orch.swiftscaffoldlib,
                                           "scaffold_spm_package",
                                           fake_scaffold):
            orch._hook_library_mining(
                cfg, self.app, self.app_dir, {}, {},
                key="extraction_candidates", md_path=None, transcript=block,
                final_output="", coord="claude", active=["claude"],
                is_build=False, is_verify_repair=False, allow_writes=False,
                _needs_vlabel=False)
        return scaffolded["called"]

    def test_writes_none_exec_true_blocks_the_scaffold_write(self):
        self.assertFalse(
            self._run_hook({"writes": "none", "exec": True, "external": False}),
            "a writes-denied section must not scaffold even with exec=true")

    def test_writes_workspace_exec_true_allows_scaffold(self):
        self.assertTrue(
            self._run_hook({"writes": "workspace", "exec": True,
                            "external": False}))

    def test_flat_legacy_allows_scaffold(self):
        self.assertTrue(self._run_hook(None))


if __name__ == "__main__":
    unittest.main()


class TestLibraryMiningBothGates(unittest.TestCase):
    """Review regression: the library_mining scaffold WRITES and the build
    EXECS — a section must have BOTH capabilities, not just exec."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _caps(self, writes, exc):
        return {"writes": writes, "exec": exc, "external": False}

    def test_writes_none_exec_true_denies_writes(self):
        cfg = _cfg(self.root, os.path.join(self.root, "p", "ideas", "c"))
        os.makedirs(cfg["_app_dir"])
        # {writes:none, exec:true}: exec allowed, writes denied -> the
        # scaffold (a write) must be blocked even though exec is allowed.
        import unittest.mock
        with unittest.mock.patch.object(orch, "_section_capabilities",
                                        lambda c: self._caps("none", True)):
            self.assertFalse(orch._section_writes_allowed(cfg))
            self.assertTrue(orch._section_exec_allowed(cfg))

    def test_writes_workspace_exec_true_allows_both(self):
        cfg = _cfg(self.root, os.path.join(self.root, "p", "ideas", "c"))
        os.makedirs(cfg["_app_dir"])
        import unittest.mock
        with unittest.mock.patch.object(orch, "_section_capabilities",
                                        lambda c: self._caps("workspace", True)):
            self.assertTrue(orch._section_writes_allowed(cfg))
            self.assertTrue(orch._section_exec_allowed(cfg))

    def test_flat_legacy_allows_both(self):
        cfg = _cfg(self.root, os.path.join(self.root, "flat"))
        os.makedirs(cfg["_app_dir"])
        self.assertTrue(orch._section_writes_allowed(cfg))
        self.assertTrue(orch._section_exec_allowed(cfg))
