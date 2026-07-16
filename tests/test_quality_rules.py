"""Tests for the Vibe-Coding-Quality-Rulebook enforcement:
(1) the distilled agent quality rules injected into the build prompt, and
(2) the designlint 'empty_action' fake-feature check.

The rulebook's own thesis is "don't trust a self-report that something is done —
verify it", so the highest-leverage rules are the ones turned into a mechanical
gate rather than prose. Both surfaces are covered here.
"""
import os
import tempfile
import unittest

import orchestrator as orch
import designlint


class TestQualityRulesInBuildPrompt(unittest.TestCase):
    def _build_extra(self, code_changes=True):
        cfg = {"runtime": {"build_code_changes_enabled": code_changes},
               "ios": {"development_team": "ABC123"}}
        return orch.phase_extra(cfg, "build_coordination")

    def test_build_phase_carries_quality_rules(self):
        extra = self._build_extra(code_changes=True)
        self.assertIn("PRODUCT-QUALITY RULES", extra)

    def test_covers_the_core_non_negotiables(self):
        extra = self._build_extra(code_changes=True)
        # The rulebook's highest-impact rules must actually reach the builder.
        self.assertIn("must never lie", extra)              # Rule 2
        self.assertIn("empty state", extra)                 # Rule 3 (no fake data)
        self.assertIn("EXPLICIT state model", extra)        # Rule 4
        self.assertIn("never clip", extra)                  # Rule 5 (adaptive layout)
        self.assertIn("survive relaunch", extra)            # Rule 6 (real persistence)
        self.assertIn("duplicate submissions", extra)       # Rule 4.3
        self.assertIn("Accessibility", extra)               # Section 10
        self.assertIn("placeholder", extra)                 # Rule 16 / honest reporting

    def test_absent_when_code_changes_disabled(self):
        # Discussion-only build_coordination (no file writes) shouldn't carry the
        # build rules — there's nothing being built to grade.
        self.assertNotIn("PRODUCT-QUALITY RULES", self._build_extra(code_changes=False))

    def test_other_phases_do_not_get_build_rules(self):
        cfg = {"_workflow_target": "app"}
        self.assertNotIn("PRODUCT-QUALITY RULES", orch.phase_extra(cfg, "tech_specs"))
        self.assertNotIn("PRODUCT-QUALITY RULES", orch.phase_extra(cfg, "task_assignments"))


class TestEmptyActionLint(unittest.TestCase):
    """designlint's fake-feature check (Rulebook §16): a control whose action is
    an empty closure is a decorative control masquerading as a real one."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _scan(self, filename, src):
        with open(os.path.join(self.d, filename), "w", encoding="utf-8") as fh:
            fh.write(src)
        errors, warnings = designlint.scan(self.d, self.d)
        return errors, warnings

    def test_flags_empty_button_action_closure(self):
        _e, w = self._scan("V.swift",
                           'struct V: View { var body: some View {\n'
                           '  Button("Save") { }\n'
                           '} }\n')
        self.assertTrue(any(x["rule"] == "empty_action" for x in w),
                        "empty Button trailing closure not flagged: %s" % w)

    def test_flags_empty_action_argument(self):
        _e, w = self._scan("V.swift",
                           'struct V: View { var body: some View {\n'
                           '  Button(action: {}) { Text("Go") }\n'
                           '} }\n')
        self.assertTrue(any(x["rule"] == "empty_action" for x in w))

    def test_real_action_not_flagged(self):
        _e, w = self._scan("V.swift",
                           'struct V: View { var body: some View {\n'
                           '  Button("Save") { store.save() }\n'
                           '} }\n')
        self.assertFalse(any(x["rule"] == "empty_action" for x in w),
                         "a real action was wrongly flagged: %s" % w)

    def test_empty_action_is_a_warning_not_a_hard_error(self):
        # Soft signal (like todo_marker): surfaces without hard-blocking a build,
        # until it's proven low-false-positive enough to promote to an error.
        e, _w = self._scan("V.swift", 'Button("x") { }\n')
        self.assertFalse(any(x["rule"] == "empty_action" for x in e))


if __name__ == "__main__":
    unittest.main()
