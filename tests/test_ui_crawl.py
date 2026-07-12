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
        flows = json.load(open(os.path.join(self.app_dir, "flows.json")))["flows"]
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
                one = json.load(open(flows_path))["flows"][0]
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


class TestFlowsContract(unittest.TestCase):
    def test_parse_flows_blocks(self):
        text = ("Prose...\n```flows-json\n"
                + json.dumps({"flows": [
                    {"name": "add dish", "steps": [{"tap": "Add"}]},
                    {"name": "bad", "steps": []}]})
                + "\n```\nmore prose")
        flows, errors = orch.parse_flows_blocks(text)
        self.assertEqual([f["name"] for f in flows], ["add dish"])
        self.assertTrue(any("bad" in e for e in errors))

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
