"""Tests for the Vibe-Coding-Quality-Rulebook enforcement:
(1) the distilled agent quality rules injected into the build prompt, and
(2) the designlint 'empty_action' fake-feature check.

The rulebook's own thesis is "don't trust a self-report that something is done —
verify it", so the highest-leverage rules are the ones turned into a mechanical
gate rather than prose. Both surfaces are covered here.
"""
import json
import os
import tempfile
import unittest
import unittest.mock

import orchestrator as orch
import designlint
import knowledge as knowlib
import visualqa


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


class TestDesignLintCommentStripping(unittest.TestCase):
    """Code-token checks (inline_color/raw_font_size/empty_action) must match
    REAL code, not a forbidden pattern appearing in a comment or string. A
    `.font(.system(size:))` mentioned in a doc comment that documented AVOIDING
    it hard-failed a clean build (steep, live)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _errs(self, src, name="V.swift"):
        with open(os.path.join(self.d, name), "w", encoding="utf-8") as fh:
            fh.write(src)
        e, _w = designlint.scan(self.d, self.d)
        return e

    def test_raw_font_in_doc_comment_not_flagged(self):
        # The exact live false positive.
        src = ('/// Uses `.imageScale` rather than `.font(.system(size:))` so it\n'
               '/// stays outside the design-lint gate.\n'
               'struct V: View { var body: some View { Text("x") } }\n')
        self.assertFalse(any(x["rule"] == "raw_font_size" for x in self._errs(src)))

    def test_raw_font_in_real_code_still_flagged(self):
        # No false negative: a genuine violation still fails.
        src = 'Text("x").font(.system(size: 17))\n'
        self.assertTrue(any(x["rule"] == "raw_font_size" for x in self._errs(src)))

    def test_inline_color_in_line_comment_not_flagged(self):
        self.assertFalse(any(x["rule"] == "inline_color"
                             for x in self._errs('// avoid Color(red: 1, green: 0, blue: 0)\n')))

    def test_inline_color_in_string_not_flagged(self):
        self.assertFalse(any(x["rule"] == "inline_color"
                             for x in self._errs('let hint = "do not use Color(red:)"\n')))

    def test_inline_color_in_real_code_still_flagged(self):
        self.assertTrue(any(x["rule"] == "inline_color"
                            for x in self._errs('let c = Color(red: 1, green: 0, blue: 0)\n')))

    def test_pattern_in_block_comment_across_lines_not_flagged(self):
        src = ('/*\n'
               ' Color(red: 1) and .font(.system(size: 12)) are both banned;\n'
               ' this block explains why.\n'
               '*/\n'
               'struct V: View { var body: some View { Text("x") } }\n')
        e = self._errs(src)
        self.assertFalse(any(x["rule"] in ("inline_color", "raw_font_size") for x in e))

    def test_empty_action_still_flags_real_button_after_string_strip(self):
        # Stripping the label string leaves Button() { } — still a dead control.
        _w = self._errs('Button("Save") { }\n')
        # empty_action is a warning; scan() returns (errors, warnings)
        with open(os.path.join(self.d, "W.swift"), "w") as fh:
            fh.write('Button("Save") { }\n')
        _e, warns = designlint.scan(self.d, self.d)
        self.assertTrue(any(x["rule"] == "empty_action" for x in warns))

    def test_empty_action_in_comment_not_flagged(self):
        with open(os.path.join(self.d, "V.swift"), "w") as fh:
            fh.write('// example: Button("x") { }\n')
        _e, warns = designlint.scan(self.d, self.d)
        self.assertFalse(any(x["rule"] == "empty_action" for x in warns))

    def test_todo_in_comment_still_detected(self):
        # todo_marker keeps the raw line — comments are its whole point.
        with open(os.path.join(self.d, "V.swift"), "w") as fh:
            fh.write('// TODO: wire this up\n')
        _e, warns = designlint.scan(self.d, self.d)
        self.assertTrue(any(x["rule"] == "todo_marker" for x in warns))


class TestLaunchScreenLint(unittest.TestCase):
    """designlint's letterboxing check (M-004): an iOS app target with no
    launch-screen configuration anywhere renders in a legacy-size, letterboxed
    window — observed live cropping a timer screen's Start button clean off
    the bottom while the Swift code itself was fine. Hard error."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _mk(self, rel, content=""):
        p = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _mkproj(self, pbx_content=""):
        os.makedirs(os.path.join(self.d, "App.xcodeproj"), exist_ok=True)
        self._mk("App.xcodeproj/project.pbxproj", pbx_content)

    def test_app_without_any_launch_config_is_hard_error(self):
        self._mkproj("/* no launch keys */")
        self._mk("Info.plist", "<plist><dict><key>CFBundleName</key>"
                               "<string>App</string></dict></plist>")
        errors, _w = designlint.scan(self.d, self.d)
        self.assertTrue(any(x["rule"] == "missing_launch_screen" for x in errors),
                        "letterboxing config gap not flagged: %s" % errors)

    def test_uilaunchscreen_in_plist_is_clean(self):
        self._mkproj()
        self._mk("Info.plist", "<plist><dict><key>UILaunchScreen</key>"
                               "<dict/></dict></plist>")
        errors, _w = designlint.scan(self.d, self.d)
        self.assertFalse(any(x["rule"] == "missing_launch_screen" for x in errors))

    def test_generated_infoplist_key_in_pbxproj_is_clean(self):
        # GENERATE_INFOPLIST_FILE projects carry the key as a build setting.
        self._mkproj("INFOPLIST_KEY_UILaunchScreen_Generation = YES;")
        errors, _w = designlint.scan(self.d, self.d)
        self.assertFalse(any(x["rule"] == "missing_launch_screen" for x in errors))

    def test_launchscreen_storyboard_is_clean(self):
        self._mkproj()
        self._mk("Info.plist", "<plist><dict/></plist>")
        self._mk("LaunchScreen.storyboard", "<document/>")
        errors, _w = designlint.scan(self.d, self.d)
        self.assertFalse(any(x["rule"] == "missing_launch_screen" for x in errors))

    def test_spm_library_without_xcodeproj_is_skipped(self):
        # No app target -> nothing to letterbox (verify.py owns the
        # no-runnable-app-target failure).
        self._mk("Package.swift", "// swift-tools-version:5.9")
        errors, _w = designlint.scan(self.d, self.d)
        self.assertFalse(any(x["rule"] == "missing_launch_screen" for x in errors))


class TestOverlapRuleEverywhere(unittest.TestCase):
    """The layout-collision rule (from the observed Gloam text-overlap defect)
    must live in every place that shapes or grades a build — the build prompt,
    the machine rules, the visual-QA gate, and the retrievable mistake log —
    so the same 'silly mistake' can't be reintroduced in a future round."""

    def test_build_prompt_forbids_overlap(self):
        cfg = {"runtime": {"build_code_changes_enabled": True}, "ios": {}}
        extra = orch.phase_extra(cfg, "build_coordination")
        self.assertIn("OVERLAP", extra)
        self.assertIn("minimum gap", extra)

    def test_phase_rules_json_forbids_overlap(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "phase_rules.json"), encoding="utf-8") as fh:
            rules = json.load(fh)["global_app_rules"]
        blob = " ".join(rules).lower()
        self.assertIn("overlap", blob)
        # and the interface-must-not-lie rule landed too
        self.assertTrue(any("never claim an outcome" in r.lower() for r in rules))

    def test_visual_qa_rubric_flags_overlap_as_bad(self):
        # _ask_one builds the grader prompt and POSTs it; capture the request
        # body and assert the rubric now tells the model overlap = BAD.
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"response": "OK looks fine"}).encode()

        def fake_urlopen(req, timeout=0):
            captured["body"] = req.data.decode("utf-8")
            return _Resp()

        with unittest.mock.patch.object(visualqa.urllib.request, "urlopen",
                                        side_effect=fake_urlopen):
            visualqa._ask_one("m", "b64", "light", "a solar day app")
        prompt = json.loads(captured["body"])["prompt"].lower()
        self.assertIn("overlap", prompt)
        self.assertIn("bad", prompt)

    def test_observed_mistakes_log_is_retrievable_for_ios_builds(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        res = knowlib.retrieve(
            here, "ios",
            "SwiftUI layout placing event times on a timeline, overlapping text, design",
            max_chars=8000, top_k=4)
        self.assertTrue("Observed Mistakes" in res or "M-001" in res,
                        "the curated mistake log did not surface for an iOS layout query")

    def test_mistake_log_file_exists_with_the_overlap_entry(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(here, "knowledge", "ios", "observed-mistakes.md")
        self.assertTrue(os.path.exists(p))
        with open(p, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("M-001", body)
        self.assertIn("overlap", body.lower())


if __name__ == "__main__":
    unittest.main()
