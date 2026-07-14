"""The 17-task batch's load-bearing new logic: design/dependency lint,
fleet learning (incidents/ratings/presort/exemplars), Definition of Done
tiers, the requirements-json contract, vertical-slice wave layering, rolling
summary preference, and the eval-harness scorer."""
import json
import os
import tempfile
import unittest

import completeness as complib
import designlint as dlint
import evalharness as evallib
import fleetlearn as fllib
import orchestrator as orch
import verify as verifylib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cget(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class TestDesignLint(unittest.TestCase):
    def setUp(self):
        self.build = tempfile.mkdtemp()
        self.here = tempfile.mkdtemp()

    def _w(self, rel, text):
        p = os.path.join(self.build, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_inline_color_and_font_are_errors_outside_ds(self):
        self._w("Views/Home.swift",
                'let c = Color(red: 0.1, green: 0.2, blue: 0.3)\n'
                'Text("x").font(.system(size: 17))\n')
        self._w("DesignSystem.swift",
                'let accent = Color(red: 1, green: 0, blue: 0)\n')
        errors, _w = dlint.scan(self.build, self.here)
        rules = sorted(e["rule"] for e in errors)
        self.assertEqual(rules, ["inline_color", "raw_font_size"])
        self.assertTrue(all(e["file"] != "DesignSystem.swift" for e in errors))

    def test_missing_design_system_and_todo_warn(self):
        for n in ("A", "B", "C"):
            self._w(n + ".swift", "// TODO: finish\nstruct %s {}\n" % n)
        errors, warnings = dlint.scan(self.build, self.here)
        self.assertEqual(errors, [])
        rules = {w["rule"] for w in warnings}
        self.assertIn("missing_design_system", rules)
        self.assertIn("todo_marker", rules)

    def test_banned_and_unlisted_packages(self):
        with open(os.path.join(self.here, "tech_stack.json"), "w") as fh:
            json.dump({"allowed": [{"name": "swift-collections"}],
                       "banned": [{"name": "Alamofire", "why": "no"}]}, fh)
        self._w("Package.swift",
                'dependencies: [\n'
                ' .package(url: "https://github.com/Alamofire/Alamofire.git", from: "5.0.0"),\n'
                ' .package(url: "https://github.com/apple/swift-algorithms", from: "1.0.0"),\n'
                ']\n')
        errors, warnings = dlint.scan(self.build, self.here)
        self.assertEqual([e["rule"] for e in errors], ["banned_package"])
        self.assertIn("unlisted_package", {w["rule"] for w in warnings})

    def test_gate_fails_on_errors_passes_clean(self):
        app_dir = tempfile.mkdtemp()
        bd = os.path.join(app_dir, "app_build")
        os.makedirs(bd)
        with open(os.path.join(bd, "Home.swift"), "w") as fh:
            fh.write("let c = Color(red: 1, green: 1, blue: 1)\n")
        cfg = {"runtime": {"design_lint_enabled": True}}
        reason = dlint.run_design_lint(cfg, _cget, lambda m: None, "x",
                                       app_dir, self.here)
        self.assertIn("inline_color", reason)
        saved = json.load(open(os.path.join(app_dir, "docs",
                                            "design_lint.json")))
        self.assertEqual(len(saved["errors"]), 1)
        os.remove(os.path.join(bd, "Home.swift"))
        with open(os.path.join(bd, "Clean.swift"), "w") as fh:
            fh.write("struct Clean {}\n")
        self.assertIsNone(dlint.run_design_lint(cfg, _cget, lambda m: None,
                                                "x", app_dir, self.here))


class TestFleetLearn(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()

    def test_incident_attribution_defaults(self):
        e = fllib.record_incident(self.app_dir, "visual_qa", "blank screen")
        self.assertIn("design_handoff", e["blamed_phases"])
        loaded = fllib.load_incidents(self.app_dir)
        self.assertEqual(loaded[0]["gate"], "visual_qa")

    def test_rating_roundtrip_and_clear(self):
        self.assertTrue(fllib.save_rating(self.app_dir, "bad", "ugly nav"))
        self.assertEqual(fllib.load_rating(self.app_dir)["verdict"], "bad")
        self.assertTrue(fllib.save_rating(self.app_dir, None))
        self.assertIsNone(fllib.load_rating(self.app_dir))

    def test_presort_ranks_failures_first(self):
        root = tempfile.mkdtemp()
        for name, state in (("clean", {"done": True}),
                            ("broken", {"done": False, "error": "boom",
                                        "release_gate_repairs": 2})):
            d = os.path.join(root, name, "initial_prompt")
            os.makedirs(d)
            open(os.path.join(d, "initial_prompt.md"), "w").write("p")
            with open(os.path.join(root, name, "agent_state.json"), "w") as fh:
                json.dump(state, fh)
        rows = fllib.presort(root)
        self.assertEqual(rows[0]["app"], "broken")
        self.assertEqual(rows[0]["proposed"], "bad")
        self.assertEqual(rows[-1]["proposed"], "good")

    def test_export_exemplars(self):
        here = tempfile.mkdtemp()
        with open(os.path.join(self.app_dir, "agent_state.json"), "w") as fh:
            json.dump({"phase_outputs": {
                "design_handoff": "x" * 300, "app_features": "y" * 300,
                "tiny": "z"}}, fh)
        written = fllib.export_exemplars(self.app_dir, here, "demo")
        names = {os.path.basename(os.path.dirname(w)) for w in written}
        self.assertEqual(names, {"design_handoff", "app_features"})

    def test_ledger_written_from_incidents(self):
        root = tempfile.mkdtemp()
        here = tempfile.mkdtemp()
        d = os.path.join(root, "p1", "initial_prompt")
        os.makedirs(d)
        open(os.path.join(d, "initial_prompt.md"), "w").write("p")
        fllib.record_incident(os.path.join(root, "p1"), "design_lint",
                              "inline colors in 3 files")
        path, clusters = fllib.build_ledger(root, here, model="")
        self.assertTrue(path.endswith("anti_patterns.md"))
        body = open(path).read()
        self.assertIn("design_lint", body)
        self.assertEqual(clusters, 1)


class TestDefinitionOfDone(unittest.TestCase):
    def test_tiers_inherit(self):
        proto = complib.dod_items(HERE, "prototype")
        v1 = complib.dod_items(HERE, "v1")
        self.assertTrue(set(proto).issubset(set(v1)))
        self.assertGreater(len(v1), len(proto))

    def test_render_and_unknown_tier(self):
        block = complib.render_dod(HERE, "nonsense")
        self.assertIn("DEFINITION OF DONE", block)
        self.assertIn("tier: v1", block)

    def test_file_override(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "definition_of_done.json"), "w") as fh:
            json.dump({"prototype": ["only this"]}, fh)
        self.assertEqual(complib.dod_items(d, "prototype"), ["only this"])


class TestRequirementsContract(unittest.TestCase):
    def test_parse_and_dedupe(self):
        text = ("```requirements-json\n"
                + json.dumps({"requirements": [
                    {"id": "R-001", "text": "add habits", "core": True},
                    {"id": "R-002", "text": "streak view"},
                    {"id": "R-001", "text": "add habits (revised)"},
                    {"text": "no id"}]})
                + "\n```")
        reqs, errors = orch.parse_requirements_blocks(text)
        byid = {r["id"]: r for r in reqs}
        self.assertEqual(len(reqs), 2)
        self.assertIn("revised", byid["R-001"]["text"])   # last emission wins
        self.assertTrue(byid["R-002"]["core"])            # core defaults True
        self.assertTrue(errors)

    def test_persist_load_roundtrip(self):
        app_dir = tempfile.mkdtemp()
        orch.persist_requirements(app_dir, [{"id": "R-001", "text": "t",
                                             "core": True}])
        self.assertEqual(orch.load_requirements(app_dir)[0]["id"], "R-001")


class TestVerticalWaves(unittest.TestCase):
    def _t(self, tid, deps=()):
        return {"id": tid, "title": tid, "owner_lane": "primary_ui",
                "depends_on": list(deps)}

    def test_topo_layers(self):
        backlog = [self._t("T-1"), self._t("T-2"),
                   self._t("T-3", ["T-1"]), self._t("T-4", ["T-2", "T-3"])]
        waves = orch._task_waves(backlog)
        ids = [[t["id"] for t in w] for w in waves]
        self.assertEqual(ids, [["T-1", "T-2"], ["T-3"], ["T-4"]])

    def test_small_backlogs_skip_slicing(self):
        self.assertEqual(orch._task_waves([self._t("T-1"), self._t("T-2")]), [])

    def test_cycle_becomes_final_wave(self):
        backlog = [self._t("T-1"), self._t("T-2"),
                   self._t("T-3", ["T-4"]), self._t("T-4", ["T-3"])]
        waves = orch._task_waves(backlog)
        self.assertEqual([t["id"] for t in waves[-1]], ["T-3", "T-4"])

    def test_unknown_deps_count_satisfied(self):
        backlog = [self._t("T-%d" % i, ["GHOST"]) for i in range(1, 5)]
        waves = orch._task_waves(backlog)
        self.assertEqual(waves, [])   # one wave only -> no slicing


class TestRollingSummaryPreference(unittest.TestCase):
    def test_summary_replaces_transcript(self):
        app_dir = tempfile.mkdtemp()
        phase = ("alpha", "alpha", "alpha.md", "p")
        d = os.path.join(app_dir, "alpha")
        os.makedirs(d)
        open(os.path.join(d, "alpha.md"), "w").write("RAW TRANSCRIPT TEXT")
        phases = [phase]
        full = orch.prior_discussion_context(app_dir, phases, ["alpha"])
        self.assertIn("RAW TRANSCRIPT", full)
        summed = orch.prior_discussion_context(
            app_dir, phases, ["alpha"], summaries={"alpha": "THE SUMMARY"})
        self.assertIn("THE SUMMARY", summed)
        self.assertNotIn("RAW TRANSCRIPT", summed)


class TestEvalHarness(unittest.TestCase):
    def test_score_project_composite(self):
        app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(app_dir, "docs"))
        # verify.py's real on-disk shape is a bare LIST of records (written by
        # persist_verify_result), not {"attempts": [...]}. Use the real writer
        # so this test can't drift from production again.
        verifylib.persist_verify_result(app_dir, "release_gate",
                                        {"ran": True, "ok": True})
        json.dump({"verdict": "PASS", "score": 80},
                  open(os.path.join(app_dir, "docs", "adherence.json"), "w"))
        json.dump({"verdict": "PASS", "score": 100},
                  open(os.path.join(app_dir, "docs", "visual_qa.json"), "w"))
        json.dump({"flows": [{"passed": True}, {"passed": True}],
                   "crashes": [], "dead_taps": []},
                  open(os.path.join(app_dir, "docs", "ui_crawl.json"), "w"))
        json.dump({"errors": [], "warnings": []},
                  open(os.path.join(app_dir, "docs", "design_lint.json"), "w"))
        json.dump({"done": True},
                  open(os.path.join(app_dir, "agent_state.json"), "w"))
        r = evallib.score_project(app_dir)
        # 30 compile + 20 adherence + 15 visual + 15 flows + 10 crawl + 5 lint
        self.assertEqual(r["composite"], 95)
        self.assertTrue(r["compile_ok"])
        self.assertEqual(r["flows"], "2/2")

    def test_missing_evidence_is_not_scored_as_zero_defects(self):
        # A project with NO crawl/lint reports at all (gates never ran) must
        # not collect the crawl-hygiene/lint points that a genuinely clean
        # pass would earn — missing evidence != zero defects.
        app_dir = tempfile.mkdtemp()
        json.dump({"done": True},
                  open(os.path.join(app_dir, "agent_state.json"), "w"))
        r = evallib.score_project(app_dir)
        self.assertEqual(r["composite"], 0)
        self.assertFalse(r["compile_ok"])
        self.assertEqual(r["crashes"], "n/a")
        self.assertEqual(r["dead_buttons"], "n/a")
        self.assertEqual(r["lint_errors"], "n/a")


if __name__ == "__main__":
    unittest.main()


class TestBuildLaneLocals(unittest.TestCase):
    def _cfg(self, allow_locals):
        return {"runtime": {"build_parallel_workers": 3,
                            "locals_in_build_lanes": allow_locals}}

    def test_locals_excluded_from_lanes_by_default(self):
        real = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True
        try:
            roster = orch.build_worker_roster(
                self._cfg(False),
                ["codex", "claude", "local:qwen3-coder:30b"])
            agents = [w["agent"] for w in roster]
            self.assertNotIn("local:qwen3-coder:30b", agents)
            self.assertIn("codex", agents)
            self.assertIn("claude", agents)
        finally:
            orch._agent_available = real

    def test_opt_in_keeps_locals_in_lanes(self):
        real = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True
        try:
            roster = orch.build_worker_roster(
                self._cfg(True),
                ["codex", "claude", "local:qwen3-coder:30b"])
            self.assertIn("local:qwen3-coder:30b",
                          [w["agent"] for w in roster])
        finally:
            orch._agent_available = real

    def test_all_local_roster_still_builds(self):
        """Cloud-only filter must never strand a local-only setup."""
        real = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True
        try:
            roster = orch.build_worker_roster(
                self._cfg(False), ["local:qwen2.5-coder:14b"])
            self.assertTrue(roster)
            self.assertEqual(roster[0]["agent"], "local:qwen2.5-coder:14b")
        finally:
            orch._agent_available = real
