"""V3 7.4a: per-section capability manifest — default-deny, validation,
and the exceeds-workspace-only predicate the Conductor gates on.
"""
import json
import os
import shutil
import tempfile
import unittest

import sections as seclib


class TestNormalize(unittest.TestCase):
    def test_absent_block_is_workspace_only_default(self):
        self.assertEqual(seclib.normalize_capabilities(None),
                         seclib.DEFAULT_CAPABILITIES)
        self.assertFalse(seclib.exceeds_workspace_only(
            seclib.normalize_capabilities(None)))

    def test_valid_escalation_preserved(self):
        caps = seclib.normalize_capabilities(
            {"writes": "none", "exec": True, "external": True})
        self.assertEqual(caps, {"writes": "none", "exec": True,
                                "external": True})
        self.assertTrue(seclib.exceeds_workspace_only(caps))

    def test_invalid_values_deny_default_with_warning(self):
        warns = []
        caps = seclib.normalize_capabilities(
            {"writes": "repo", "exec": "yes", "external": 1},
            on_warn=warns.append)
        # deny-safe: bad writes -> workspace, bad flags -> False
        self.assertEqual(caps, {"writes": "workspace", "exec": False,
                                "external": False})
        self.assertEqual(len(warns), 3)

    def test_non_object_block_defaults(self):
        self.assertEqual(seclib.normalize_capabilities([1, 2]),
                         seclib.DEFAULT_CAPABILITIES)

    def test_workspace_writes_alone_does_not_exceed(self):
        self.assertFalse(seclib.exceeds_workspace_only(
            {"writes": "workspace", "exec": False, "external": False}))


class TestSectionLoad(unittest.TestCase):
    def setUp(self):
        self.orch = tempfile.mkdtemp()
        self.secdir = os.path.join(self.orch, "sections", "ideas")
        os.makedirs(self.secdir)

    def tearDown(self):
        shutil.rmtree(self.orch, ignore_errors=True)

    def _write(self, obj):
        with open(os.path.join(self.secdir, "section.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_section_carries_capabilities_and_roundtrips(self):
        self._write({"id": "ideas", "title": "Ideas", "workflow": "chat_ideas",
                     "capabilities": {"writes": "workspace", "exec": True,
                                      "external": False}})
        sec = seclib.load_section("ideas", self.orch)
        self.assertTrue(sec.capabilities["exec"])
        self.assertIn("capabilities", sec.to_json())
        self.assertTrue(seclib.exceeds_workspace_only(sec.capabilities))

    def test_no_block_defaults_to_deny_safe(self):
        self._write({"id": "ideas", "title": "Ideas",
                     "workflow": "chat_ideas"})
        sec = seclib.load_section("ideas", self.orch)
        self.assertEqual(sec.capabilities, seclib.DEFAULT_CAPABILITIES)


class TestLint(unittest.TestCase):
    def setUp(self):
        self.orch = tempfile.mkdtemp()
        self.secdir = os.path.join(self.orch, "sections", "ideas")
        os.makedirs(self.secdir)

    def tearDown(self):
        shutil.rmtree(self.orch, ignore_errors=True)

    def _write(self, obj):
        with open(os.path.join(self.secdir, "section.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_invalid_capability_flagged_as_warning(self):
        self._write({"id": "ideas", "title": "Ideas", "workflow": "chat_ideas",
                     "capabilities": {"writes": "repo"}})
        report = seclib.lint_section("ideas", self.orch)
        cap_warns = [r for r in report if r["field"] == "capabilities"]
        self.assertTrue(any("invalid" in r["message"] for r in cap_warns))
        self.assertTrue(all(r["severity"] == "warning" for r in cap_warns))

    def test_exceeds_workspace_noted(self):
        self._write({"id": "ideas", "title": "Ideas", "workflow": "chat_ideas",
                     "capabilities": {"external": True}})
        report = seclib.lint_section("ideas", self.orch)
        self.assertTrue(any("require approval" in r["message"]
                            for r in report))


if __name__ == "__main__":
    unittest.main()
