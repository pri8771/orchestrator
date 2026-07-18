"""UI crawl gate (uicrawl.py + the flows-json contract): report policy
(crash/flow failures fail, dead buttons warn), self-learning regression
flows, skip paths, and the flows-json parse/persist round trip — with the
runner and simulator faked so the suite never needs Xcode."""
import json
import os
import tempfile
import unittest

import orchestrator as orch
import uicrawl as uc


def _cget(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class TestRegressionLearning(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()

    def test_crash_path_becomes_flow(self):
        crashes = [{"path": [{"kind": "button", "id": "", "label": "Get Started"},
                             {"kind": "cell", "id": "dish_row", "label": "Roast"}],
                    "tapped": {"kind": "cell", "id": "dish_row", "label": "Roast"}}]
        added = uc.append_regression_flows(self.app_dir, crashes)
        self.assertEqual(len(added), 1)
        with open(os.path.join(self.app_dir, "flows.json")) as fh:
            flows = json.load(fh)["flows"]
        self.assertEqual(flows[0]["origin"], "ui_crawl_crash")
        self.assertEqual(flows[0]["steps"],
                         [{"tap": "Get Started"}, {"tap": "dish_row"}])
        # Re-learning the same crash is a no-op (deduped by name).
        self.assertEqual(uc.append_regression_flows(self.app_dir, crashes), [])

    def test_crash_without_path_ignored(self):
        self.assertEqual(
            uc.append_regression_flows(self.app_dir, [{"note": "left fg"}]), [])
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, "flows.json")))


class TestGatePolicy(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "app_build"), exist_ok=True)
        self._real = (uc.tools_available, uc.ensure_runner, uc.run_crawler)
        uc.tools_available = lambda: True
        uc.ensure_runner = lambda emit, here=None, build_timeout=600: "/tmp/fake.xctestrun"

    def tearDown(self):
        uc.tools_available, uc.ensure_runner, uc.run_crawler = self._real

    def _gate(self, crawl_report, cfg_runtime=None, declared=None,
              flow_failures=None):
        """declared: flows.json content; flow_failures: {name: failure_msg}
        for per-flow invocations (only=="flows")."""
        if declared:
            with open(os.path.join(self.app_dir, "flows.json"), "w") as fh:
                json.dump({"flows": declared}, fh)
        failures = flow_failures or {}

        def fake_run(xctestrun, udid, bid, report_dir, flows_path="",
                     max_screens=25, max_seconds=300, only=None, clean=True):
            if only == "flows":
                with open(flows_path) as fh:
                    one = json.load(fh)["flows"][0]
                name = one.get("name", "?")
                fail = failures.get(name)
                return {"flows": [{"name": name, "passed": fail is None,
                                   "failure": fail or ""}]}, ""
            return dict(crawl_report), ""

        uc.run_crawler = fake_run
        cfg = {"runtime": dict({"ui_crawl_enabled": True}, **(cfg_runtime or {})),
               "_sim_ctx": {"udid": "UDID-1", "bundle_id": "com.example.x",
                            "app_path": ""}}
        return uc.run_ui_crawl(cfg, _cget, lambda m: None, "x", self.app_dir,
                               {}, "prompt")

    def test_disabled_skips(self):
        cfg = {"runtime": {"ui_crawl_enabled": False}}
        self.assertIsNone(uc.run_ui_crawl(cfg, _cget, lambda m: None, "x",
                                          self.app_dir, {}, "prompt"))

    def test_clean_report_passes_and_persists(self):
        report = {"screens": [{"sig": "a"}], "edges": [], "dead_taps": [],
                  "back_violations": [], "crashes": [], "flows": []}
        self.assertIsNone(self._gate(report))
        saved = json.load(open(os.path.join(self.app_dir, "docs", "ui_crawl.json")))
        self.assertEqual(saved["screens"], 1)

    def test_crash_fails_and_learns(self):
        report = {"screens": [], "edges": [], "dead_taps": [],
                  "back_violations": [],
                  "crashes": [{"path": [{"kind": "button", "id": "",
                                         "label": "Start"}],
                               "tapped": {"kind": "button", "id": "",
                                          "label": "Start"}}],
                  "flows": []}
        reason = self._gate(report)
        self.assertIn("crashed during UI crawl", reason)
        flows = json.load(open(os.path.join(self.app_dir, "flows.json")))["flows"]
        self.assertEqual(flows[0]["steps"], [{"tap": "Start"}])

    def test_failed_flow_fails(self):
        report = {"screens": [], "edges": [], "dead_taps": [],
                  "back_violations": [], "crashes": []}
        reason = self._gate(
            report,
            declared=[{"name": "add dish", "steps": [{"tap": "Add"}]},
                      {"name": "onboard", "steps": [{"tap": "Go"}]}],
            flow_failures={"add dish": "step 3: no element ‘Save’"})
        self.assertIn("add dish", reason)
        self.assertIn("no element", reason)

    def test_failed_flow_reason_lists_actual_controls_for_repair(self):
        # The repair loop can't converge on flow/app label drift without knowing
        # the app's REAL vocabulary — the reason must surface it.
        report = {"screens": [{"id": "play.screen",
                               "elements": [{"label": "Start today’s chain"},
                                            {"id": "play.wordField"}]}],
                  "edges": [], "dead_taps": [], "back_violations": [], "crashes": []}
        reason = self._gate(
            report,
            declared=[{"name": "daily chain", "steps": [{"tap": "Play"}]}],
            flow_failures={"daily chain": "step 1: expected ‘Play’"})
        self.assertIn("daily chain", reason)
        self.assertIn("Start today’s chain", reason)   # the app's real control
        self.assertIn("play.wordField", reason)

    def test_all_flows_passing_is_clean(self):
        report = {"screens": [{"sig": "a"}], "edges": [], "dead_taps": [],
                  "back_violations": [], "crashes": []}
        self.assertIsNone(self._gate(
            report, declared=[{"name": "onboard", "steps": [{"tap": "Go"}]}]))
        saved = json.load(open(os.path.join(self.app_dir, "docs",
                                            "ui_crawl.json")))
        self.assertTrue(saved["flows"][0]["passed"])

    def test_dead_buttons_warn_by_default_fail_when_promoted(self):
        report = {"screens": [], "edges": [], "dead_taps":
                  [{"screen": "a", "tapped": {"kind": "button", "id": "",
                                              "label": "Restore Purchases"}}],
                  "back_violations": [], "crashes": [], "flows": []}
        self.assertIsNone(self._gate(report))
        reason = self._gate(report, {"ui_crawl_fail_on_dead_buttons": True})
        self.assertIn("Restore Purchases", reason)


class TestDiscoveredControls(unittest.TestCase):
    """_discovered_controls: pull the app's real interactive vocabulary out of
    the crawl report (defensive recursive walk over its varying shape)."""

    def test_collects_ids_and_labels_deduped_in_order(self):
        report = {"screens": [{"id": "play.screen",
                               "elements": [{"label": "Start"}, {"id": "wordField"}]}],
                  "dead_taps": [{"tapped": {"label": "Archive"}}],
                  "edges": [{"via": {"id": "wordField", "label": "Add"}}]}  # wordField dup
        got = uc._discovered_controls(report)
        self.assertEqual(got, ["play.screen", "Start", "wordField", "Archive", "Add"])

    def test_empty_report_yields_empty(self):
        self.assertEqual(uc._discovered_controls({}), [])
        self.assertEqual(uc._discovered_controls(None), [])

    def test_bounded_by_limit(self):
        report = {"items": [{"id": "c%d" % i} for i in range(100)]}
        self.assertEqual(len(uc._discovered_controls(report, limit=10)), 10)

    def test_ignores_non_string_and_blank(self):
        report = {"a": {"id": 5, "label": ""}, "b": {"label": "  Real  "}}
        self.assertEqual(uc._discovered_controls(report), ["Real"])

    def test_failed_flow_message_without_controls_is_plain(self):
        f = {"name": "j", "failure": "step 1: expected ‘X’"}
        msg = uc._failed_flow_message(f, [f], {})
        self.assertIn("declared user flow 'j' failed", msg)
        self.assertNotIn("ACTUAL interactive controls", msg)

    def test_failed_flow_message_counts_additional_failures(self):
        f = {"name": "j", "failure": "boom"}
        msg = uc._failed_flow_message(f, [f, f, f], {})
        self.assertIn("(+2 more)", msg)


class TestFlowsContract(unittest.TestCase):
    def test_flows_instruction_prefers_stable_identifiers(self):
        # Prevention side of the drift fix: the contract nudges toward
        # accessibilityIdentifiers so declared labels don't drift from the build.
        self.assertIn("accessibilityIdentifier", orch._FLOWS_JSON_INSTRUCTION)
        self.assertIn("PREFER", orch._FLOWS_JSON_INSTRUCTION)

    def test_flows_instruction_example_uses_dotted_identifiers(self):
        # The worked example must MODEL dotted identifiers, not visible labels —
        # the LLM copies the example, and visible-label/glyph tokens ('+',
        # 'Start') don't match a well-built app's descriptive a11y labels +
        # dotted identifiers (the observed steep flow-drift failure).
        instr = orch._FLOWS_JSON_INSTRUCTION
        # A dotted identifier token appears in the example.
        self.assertRegex(instr, r'"tap":\s*"[a-z]+\.[A-Za-z]+')
        # The old label-drift story that taught naming the visible label is gone.
        self.assertNotIn("Start today", instr)
        self.assertNotIn('"tap": "Save"', instr)
        # It explicitly forbids bare glyph tokens.
        self.assertIn("NEVER name a bare glyph", instr)

    def test_flows_instruction_supports_assert_timeouts(self):
        # A state that arrives after an app-determined delay (a 90s steep timer
        # hitting "Ready") can NEVER pass the default ~5s assert window — the
        # contract must teach the planner to declare a per-step timeout (the
        # runner honors it; observed live as a temporally-impossible flow).
        instr = orch._FLOWS_JSON_INSTRUCTION
        self.assertIn('"timeout"', instr)
        self.assertIn("timer", instr)
        self.assertIn("SHORTEST built-in duration", instr)

    def test_flows_instruction_forbids_gesture_hidden_controls(self):
        # A swipe-action/long-press-only control isn't in the hierarchy until
        # the gesture happens, and flows can't declare gestures — observed live
        # as "no tappable element 'settings.customTeaEditButton'" for a
        # swipe-action Edit button that genuinely existed.
        instr = orch._FLOWS_JSON_INSTRUCTION
        self.assertIn("swipe", instr)
        self.assertIn("long-press", instr)

    def test_parse_flows_blocks(self):
        text = ("Prose...\n```flows-json\n"
                + json.dumps({"flows": [
                    {"name": "add dish", "steps": [{"tap": "Add"}]},
                    {"name": "bad", "steps": []}]})
                + "\n```\nmore prose")
        flows, errors = orch.parse_flows_blocks(text)
        self.assertEqual([f["name"] for f in flows], ["add dish"])
        self.assertTrue(any("bad" in e for e in errors))

    def test_load_flows_missing_corrupt_and_valid(self):
        with tempfile.TemporaryDirectory() as d:
            # Missing file -> [] (never raises).
            self.assertEqual(orch.load_flows(d), [])
            # Corrupt JSON -> [].
            with open(os.path.join(d, "flows.json"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(orch.load_flows(d), [])
            # Valid -> the flows list.
            with open(os.path.join(d, "flows.json"), "w", encoding="utf-8") as fh:
                json.dump({"flows": [{"name": "x", "steps": [{"tap": "home.a"}]}]}, fh)
            self.assertEqual([f["name"] for f in orch.load_flows(d)], ["x"])

    def test_flows_contract_block_tokens_and_exclusions(self):
        # F3: the binding contract renders DISTINCT tap + type-into tokens, and
        # EXCLUDES assert_exists tokens (those name user content, not a control).
        flows = [{"name": "add", "steps": [
            {"tap": "home.addItemButton"},
            {"type": "Sample", "into": "addEdit.nameField"},
            {"tap": "addEdit.saveButton"},
            {"assert_exists": "Sample"},            # excluded (content)
            {"tap": "home.addItemButton"},          # dup -> collapsed
            {"back": True}]}]
        block = orch._flows_contract_block(flows)
        self.assertIn("BINDING CONTRACT", block)
        self.assertIn("accessibilityIdentifier", block)
        # Build workers are told the control must be reachable without a
        # gesture — a swipe-action-only affordance fails its journey.
        self.assertIn("swipe action", block)
        self.assertIn("home.addItemButton", block)
        self.assertIn("addEdit.nameField", block)
        self.assertIn("addEdit.saveButton", block)
        self.assertNotIn("Sample", block)           # assert_exists token excluded
        self.assertEqual(block.count("home.addItemButton"), 1)  # deduped

    def test_flows_contract_block_empty_when_no_control_tokens(self):
        self.assertEqual(orch._flows_contract_block([]), "")
        self.assertEqual(orch._flows_contract_block(None), "")
        # Only asserts/back -> no control tokens -> no block.
        self.assertEqual(
            orch._flows_contract_block(
                [{"name": "a", "steps": [{"assert_exists": "X"}, {"back": True}]}]),
            "")
        # Defensive against malformed shapes.
        self.assertEqual(orch._flows_contract_block(["nope", {"steps": "bad"}]), "")

    def test_worker_contract_block_appends_flow_tokens(self):
        block = orch._flows_contract_block(
            [{"name": "a", "steps": [{"tap": "home.addItemButton"}]}])
        out = orch._worker_contract_block(
            backlog=[], claimed=[], interfaces=[], flows_contract=block)
        self.assertIn("home.addItemButton", out)
        self.assertIn("BINDING CONTRACT", out)
        # No flow contract -> no flow section.
        self.assertNotIn("BINDING CONTRACT",
                         orch._worker_contract_block([], [], [], flows_contract=""))

    def test_persist_preserves_learned_regressions(self):
        app_dir = tempfile.mkdtemp()
        with open(os.path.join(app_dir, "flows.json"), "w") as fh:
            json.dump({"flows": [
                {"name": "regression: crash via Start",
                 "steps": [{"tap": "Start"}], "origin": "ui_crawl_crash"},
                {"name": "old declared", "steps": [{"tap": "Old"}]}]}, fh)
        orch.persist_flows(app_dir, [{"name": "new declared",
                                      "steps": [{"tap": "New"}]}])
        flows = json.load(open(os.path.join(app_dir, "flows.json")))["flows"]
        names = [f["name"] for f in flows]
        self.assertIn("new declared", names)
        self.assertIn("regression: crash via Start", names)   # learned: kept
        self.assertNotIn("old declared", names)               # spec: replaced


if __name__ == "__main__":
    unittest.main()
