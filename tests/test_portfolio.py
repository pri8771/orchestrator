import json
import os
import tempfile
import unittest

import orchestrator as orch
import portfolio as port
import workflows as wf


PORTFOLIO_BLOCK = """```portfolio-json
{"apps": [
  {"name": "GoEasy", "slug": "goeasy", "category": "6-8 health",
   "one_sentence_promise": "Tell athletes whether to go hard, go easy, or rest.",
   "target_user": "Structured recreational athletes",
   "claude_design_prompt": "Design a premium single-decision recovery screen.",
   "build": true, "build_priority": 1},
  {"name": "Storm Nudge", "slug": "storm-nudge", "category": "61-63 weather",
   "one_sentence_promise": "Warns when local pressure is moving fast.",
   "target_user": "People who spend time outdoors",
   "claude_design_prompt": "Design a calm pressure-change alert app.",
   "build": false}
]}
```"""


class TestSlugify(unittest.TestCase):
    def test_long_name_capped_below_filesystem_limit(self):
        long_name = "A " * 200  # an agent-emitted full sentence, not a short name
        slug = port.slugify(long_name)
        self.assertLessEqual(len(slug), port.MAX_SLUG_LEN)
        self.assertTrue(slug)

    def test_cap_does_not_leave_a_trailing_dash(self):
        slug = port.slugify("x" * (port.MAX_SLUG_LEN - 1) + "-y-z-z-z-z")
        self.assertFalse(slug.endswith("-"))


class TestPortfolioExpansion(unittest.TestCase):
    def test_parse_portfolio_manifest(self):
        manifest, errors = port.parse_portfolio_manifest(PORTFOLIO_BLOCK)
        self.assertEqual(errors, [])
        self.assertEqual([a["slug"] for a in manifest["apps"]], ["goeasy", "storm-nudge"])
        self.assertTrue(manifest["apps"][0]["build"])
        self.assertFalse(manifest["apps"][1]["build"])

    def test_materialize_children_as_siblings(self):
        root = tempfile.mkdtemp()
        parent = os.path.join(root, "testing-limits")
        os.makedirs(parent)
        manifest, _errors = port.parse_portfolio_manifest(PORTFOLIO_BLOCK)

        result = port.materialize_children(
            root, "testing-limits", parent,
            "Portfolio prompt: one folder per selected app.", manifest)

        self.assertEqual(sorted(result["created"]), ["goeasy", "storm-nudge"])
        for slug in ("goeasy", "storm-nudge"):
            self.assertTrue(os.path.exists(
                os.path.join(root, slug, "initial_prompt", "initial_prompt.md")))
            self.assertTrue(os.path.exists(os.path.join(root, slug, "SPEC.md")))
            with open(os.path.join(root, slug, "portfolio_child.json"),
                      encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["parent"], "testing-limits")
        self.assertFalse(port.child_autorun_disabled(os.path.join(root, "goeasy")))
        self.assertFalse(port.child_autorun_disabled(os.path.join(root, "storm-nudge")))
        with open(os.path.join(root, "goeasy", "workflow.txt"), encoding="utf-8") as fh:
            # Build children get the purpose-built child workflow (app_build
            # minus parent-scoped phases; falls back to app_build if removed).
            self.assertEqual(fh.read().strip(), "app_build_child")
        with open(os.path.join(root, "storm-nudge", "workflow.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "app_spec")
        self.assertTrue(os.path.exists(os.path.join(parent, "portfolio_manifest.json")))

        # Idempotent resume: update existing children, do not create goeasy-2.
        result2 = port.materialize_children(
            root, "testing-limits", parent,
            "Portfolio prompt: one folder per selected app.", manifest)
        self.assertEqual(result2["created"], [])
        self.assertIn("goeasy", result2["updated"])
        self.assertFalse(os.path.exists(os.path.join(root, "goeasy-2")))

    def test_parse_manifest_string_false_build_is_spec_only(self):
        # Agents sometimes emit build as a string; "false"/"no" must not become
        # build=True via bool().
        block = ('```portfolio-json\n{"apps": ['
                 '{"name": "Foo", "slug": "foo", "build": "false"},'
                 '{"name": "Bar", "slug": "bar", "build": "no"},'
                 '{"name": "Baz", "slug": "baz", "build": "spec-only"},'
                 '{"name": "Qux", "slug": "qux", "build": "true"},'
                 '{"name": "Zap", "slug": "zap", "build": true}'
                 ']}\n```')
        manifest, errors = port.parse_portfolio_manifest(block)
        self.assertEqual(errors, [])
        by_slug = {a["slug"]: a["build"] for a in manifest["apps"]}
        self.assertEqual(by_slug, {"foo": False, "bar": False, "baz": False,
                                   "qux": True, "zap": True})

    def test_materialize_reuses_minted_variant_when_preferred_slug_is_taken(self):
        root = tempfile.mkdtemp()
        parent = os.path.join(root, "testing-limits")
        os.makedirs(parent)
        # An unrelated folder (no portfolio_child.json) squats on the slug.
        os.makedirs(os.path.join(root, "goeasy"))
        manifest, _errors = port.parse_portfolio_manifest(PORTFOLIO_BLOCK)

        result = port.materialize_children(
            root, "testing-limits", parent,
            "Portfolio prompt: one folder per selected app.", manifest)
        self.assertIn("goeasy-2", result["created"])

        # Re-running with the same manifest must reuse goeasy-2, not mint
        # goeasy-3 / goeasy-4 on every call.
        for _ in range(2):
            again = port.materialize_children(
                root, "testing-limits", parent,
                "Portfolio prompt: one folder per selected app.", manifest)
            self.assertEqual(again["created"], [])
            self.assertIn("goeasy-2", again["updated"])
        self.assertFalse(os.path.exists(os.path.join(root, "goeasy-3")))
        self.assertFalse(os.path.exists(os.path.join(root, "goeasy-4")))

    def test_materialize_forces_all_selected_apps_to_build_when_prompt_requires_it(self):
        root = tempfile.mkdtemp()
        parent = os.path.join(root, "bulk-app")
        os.makedirs(parent)
        manifest, _errors = port.parse_portfolio_manifest(PORTFOLIO_BLOCK)

        result = port.materialize_children(
            root, "bulk-app", parent,
            "Out of these categories, make production-ready apps, not just specs.",
            manifest, force_build_all=True)

        self.assertEqual(sorted(result["created"]), ["goeasy", "storm-nudge"])
        with open(os.path.join(root, "storm-nudge", "portfolio_child.json"),
                  encoding="utf-8") as fh:
            self.assertTrue(json.load(fh)["build"])
        with open(os.path.join(root, "storm-nudge", "workflow.txt"),
                  encoding="utf-8") as fh:
            # Build children get the purpose-built child workflow (app_build
            # minus parent-scoped phases; falls back to app_build if removed).
            self.assertEqual(fh.read().strip(), "app_build_child")

    def test_apply_build_plan_caps_builds_by_priority(self):
        manifest, _errors = port.parse_portfolio_manifest(PORTFOLIO_BLOCK)
        manifest["apps"].append({
            "name": "Ledger Lens",
            "slug": "ledger-lens",
            "build": True,
            "build_priority": 2,
        })
        planned = port.apply_build_plan(manifest, build_limit=2, force_build_all=True)
        built = [a["slug"] for a in planned["apps"] if a.get("build")]
        self.assertEqual(built, ["goeasy", "ledger-lens"])
        spec_only = [a["slug"] for a in planned["apps"] if not a.get("build")]
        self.assertIn("storm-nudge", spec_only)

    def test_portfolio_parent_prompt_detection(self):
        self.assertTrue(port.is_portfolio_parent_prompt(
            "Make a portfolio. For every selected app produce one folder per selected app."))
        self.assertTrue(port.is_portfolio_parent_prompt(
            "Business\nFinance\nFood & Drink\nHealth & Fitness\n"
            "Out of these categories, I want 1 unique app for each. Each app should be local-first."))
        self.assertFalse(port.is_portfolio_parent_prompt(
            "PORTFOLIO_CHILD_PROJECT: true\nBuild GoEasy."))

    def test_portfolio_detection_boundaries(self):
        # Single, ordinary app request — must NOT be classified as a portfolio.
        self.assertFalse(port.is_portfolio_parent_prompt(
            "Build me a simple weather app with a clean SwiftUI design."))
        self.assertFalse(port.is_portfolio_parent_prompt(""))
        self.assertFalse(port.is_portfolio_parent_prompt(None))
        # "portfolio" word + exactly one signal -> parent.
        self.assertTrue(port.is_portfolio_parent_prompt(
            "This is a portfolio; build multiple apps."))
        # Two distinct signals, no "portfolio" word -> parent.
        self.assertTrue(port.is_portfolio_parent_prompt(
            "There are multiple apps here; each selected app gets its own folder."))
        # Only categories, but no distributive phrase -> not enough.
        self.assertFalse(port.is_portfolio_parent_prompt(
            "Business, Finance, Weather, Sports are all interesting spaces."))
        # A child project is explicitly excluded even if it lists categories.
        self.assertFalse(port.is_portfolio_parent_prompt(
            "portfolio_child_project: true\nBusiness Finance Weather Sports, one app for each"))

    def test_heuristic_signals_are_module_constants(self):
        # The signal tables are hoisted so they can be inspected/tuned/tested.
        self.assertIn("multiple apps", port.PORTFOLIO_SIGNALS)
        self.assertIn("weather", port.APP_CATEGORY_NAMES)

    def test_prompt_requires_built_children_for_production_multi_app_request(self):
        prompt = (
            "Business\nFinance\nFood & Drink\nHealth & Fitness\n"
            "Out of these categories, I want 1 unique app for each. "
            "No, all these apps should go from 0 -> production. "
            "not just a few features or specs."
        )
        self.assertTrue(port.is_portfolio_parent_prompt(prompt))
        self.assertTrue(port.requires_built_children(prompt))
        self.assertFalse(port.requires_built_children(
            "Make a portfolio with one app for each category, spec-only for now."))

    def test_process_app_skips_spec_only_child_unless_explicit(self):
        root = tempfile.mkdtemp()
        app_dir = os.path.join(root, "spec-child")
        os.makedirs(os.path.join(app_dir, "initial_prompt"))
        with open(os.path.join(app_dir, "initial_prompt", "initial_prompt.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("PORTFOLIO_CHILD_PROJECT: true\n")
        with open(os.path.join(app_dir, port.AUTORUN_DISABLED_NAME),
                  "w", encoding="utf-8") as fh:
            fh.write("skip\n")
        called = []
        orig = orch._run_app_pipeline
        try:
            orch._run_app_pipeline = lambda *args, **kwargs: called.append(args)
            orch.process_app({"_explicit_app": False, "runtime": {}}, root, "spec-child")
            self.assertEqual(called, [])
            orch.process_app({"_explicit_app": True, "runtime": {}}, root, "spec-child")
            self.assertEqual(len(called), 1)
        finally:
            orch._run_app_pipeline = orig

    def test_skipped_parent_build_phase_records_output_without_app_build(self):
        root = tempfile.mkdtemp()
        parent = os.path.join(root, "testing-limits")
        os.makedirs(os.path.join(parent, "initial_prompt"))
        prompt = "Make a portfolio. For every selected app produce one folder per selected app."
        phase = wf.load_workflow("app_build").phase("build_coordination")
        state = orch.load_state(parent)

        reason = orch._record_skipped_phase(
            "testing-limits", parent, phase, prompt, state,
            "Skipped on portfolio parent: child builds own the app_build workflows.")

        self.assertIn("child builds", reason)
        self.assertFalse(os.path.exists(os.path.join(parent, "app_build")))
        loaded = orch.load_state(parent)
        self.assertIn("build_coordination", loaded["completed_phases"])
        self.assertIn("portfolio parent", loaded["phase_outputs"]["build_coordination"])

    def test_app_spec_workflow_has_no_build_phase(self):
        spec = wf.load_workflow("app_spec")
        self.assertIsNone(spec.build_phase)
        keys = [p.key for p in spec.phases]
        self.assertIn("design_discussion", keys)
        self.assertIn("tech_specs", keys)
        self.assertIn("project_plan", keys)
        self.assertNotIn("build_coordination", keys)


if __name__ == "__main__":
    unittest.main()
