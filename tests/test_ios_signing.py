"""fix_ios_signing deterministic pbxproj rewrite (orchestrator.py).

Regression: the old DEVELOPMENT_TEAM rule was `= (""|"")` — two identical dead
branches that matched only an empty quoted value and never inserted the key when
absent, so device-signing enforcement no-op'd on most generated projects.
"""
import os
import tempfile
import unittest

import orchestrator as orch


def _pbxproj(body):
    d = tempfile.mkdtemp()
    proj = os.path.join(d, "App.xcodeproj")
    os.makedirs(proj)
    with open(os.path.join(proj, "project.pbxproj"), "w", encoding="utf-8") as fh:
        fh.write(body)
    return d, os.path.join(proj, "project.pbxproj")


class TestFixIosSigning(unittest.TestCase):
    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_empty_team_value_is_filled(self):
        d, path = _pbxproj('buildSettings = {\n'
                           '\tDEVELOPMENT_TEAM = "";\n'
                           '\tPRODUCT_BUNDLE_IDENTIFIER = com.local.app;\n};')
        orch.fix_ios_signing(d, team="ABCDE12345")
        self.assertIn("DEVELOPMENT_TEAM = ABCDE12345;", self._read(path))

    def test_stale_team_id_is_replaced(self):
        d, path = _pbxproj('buildSettings = {\n'
                           '\tDEVELOPMENT_TEAM = OLDTEAM99;\n};')
        orch.fix_ios_signing(d, team="ABCDE12345")
        text = self._read(path)
        self.assertIn("DEVELOPMENT_TEAM = ABCDE12345;", text)
        self.assertNotIn("OLDTEAM99", text)

    def test_absent_key_is_inserted_near_bundle_id(self):
        d, path = _pbxproj('buildSettings = {\n'
                           '\tPRODUCT_BUNDLE_IDENTIFIER = com.local.app;\n};')
        changed = orch.fix_ios_signing(d, team="ABCDE12345")
        self.assertEqual(len(changed), 1)
        self.assertIn("DEVELOPMENT_TEAM = ABCDE12345;", self._read(path))

    def test_idempotent(self):
        d, path = _pbxproj('buildSettings = {\n'
                           '\tPRODUCT_BUNDLE_IDENTIFIER = com.local.app;\n};')
        orch.fix_ios_signing(d, team="ABCDE12345")
        first = self._read(path)
        second_changed = orch.fix_ios_signing(d, team="ABCDE12345")
        self.assertEqual(second_changed, [])          # no second write
        self.assertEqual(self._read(path), first)      # content stable

    def test_multiple_targets_all_get_team(self):
        body = ('buildSettings = {\n\tPRODUCT_BUNDLE_IDENTIFIER = com.local.app;\n};\n'
                'buildSettings = {\n\tPRODUCT_BUNDLE_IDENTIFIER = com.local.app.tests;\n};')
        d, path = _pbxproj(body)
        orch.fix_ios_signing(d, team="ABCDE12345")
        self.assertEqual(self._read(path).count("DEVELOPMENT_TEAM = ABCDE12345;"), 2)

    def test_non_alnum_team_is_quoted(self):
        d, path = _pbxproj('buildSettings = {\n\tDEVELOPMENT_TEAM = "";\n};')
        orch.fix_ios_signing(d, team="My Team")
        self.assertIn('DEVELOPMENT_TEAM = "My Team";', self._read(path))

    def test_also_forces_signing_flags(self):
        d, path = _pbxproj('buildSettings = {\n'
                           '\tCODE_SIGNING_ALLOWED = NO;\n'
                           '\tCODE_SIGNING_REQUIRED = NO;\n};')
        orch.fix_ios_signing(d)
        text = self._read(path)
        self.assertIn("CODE_SIGNING_ALLOWED = YES;", text)
        self.assertIn("CODE_SIGNING_REQUIRED = YES;", text)


if __name__ == "__main__":
    unittest.main()
