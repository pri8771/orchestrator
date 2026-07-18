import json
import os
import tempfile
import unittest
import unittest.mock

import orchestrator as orch
import phase_rules as pr
import workflows as wf


class TestPhaseRules(unittest.TestCase):
    def test_missing_and_malformed_rules_are_safe(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pr.render_phase_playbook(d, "app", "initial_discussion"), "")
            with open(os.path.join(d, pr.RULES_FILENAME), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(pr.load_rules(d)["phases"], {})

    def test_bad_gui_edit_rules_as_string_does_not_mis_render(self):
        # A GUI edit that turns "rules" (a list) into a bare string must not
        # reach _bullets(), which would silently iterate its characters.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, pr.RULES_FILENAME), "w", encoding="utf-8") as fh:
                json.dump({
                    "schema_version": 1, "global_app_rules": [],
                    "phases": {"tech_specs": {
                        "rules": "not a list",
                        "required_output": {"also": "not a list"},
                        "acceptance_checks": ["Real check."],
                    }},
                }, fh)
            phase = pr.load_rules(d)["phases"]["tech_specs"]
            out = pr.render_phase_playbook(d, "app", "tech_specs")
        self.assertEqual(phase["rules"], [])
        self.assertEqual(phase["required_output"], [])
        self.assertNotIn("Phase rules:", out)
        self.assertIn("Real check.", out)

    def test_load_rules_reads_disk_once_until_mtime_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, pr.RULES_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "global_app_rules": [], "phases": {}}, fh)
            real_open = open
            calls = []

            def counting_open(p, *a, **kw):
                if p == path:
                    calls.append(p)
                return real_open(p, *a, **kw)

            with unittest.mock.patch("builtins.open", counting_open):
                pr.load_rules(d)
                pr.load_rules(d)
                pr.load_rules(d)
            self.assertEqual(len(calls), 1)

    def test_same_mtime_different_size_replacement_busts_cache(self):
        # A same-second file replacement used to serve stale content because
        # the cache key was mtime alone; keying on (mtime_ns, size) catches a
        # replacement whose length differs even with an identical mtime.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, pr.RULES_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "global_app_rules": ["old rule"],
                           "phases": {}}, fh)
            st = os.stat(path)
            self.assertEqual(pr.load_rules(d)["global_app_rules"], ["old rule"])  # prime
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1,
                           "global_app_rules": ["a longer replacement rule"],
                           "phases": {}}, fh)
            # Pin the mtime back to the original — simulates a same-second
            # replacement that a coarse mtime key can't distinguish.
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertEqual(pr.load_rules(d)["global_app_rules"],
                             ["a longer replacement rule"])

    def test_render_app_phase_playbook(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, pr.RULES_FILENAME), "w", encoding="utf-8") as fh:
                json.dump({
                    "schema_version": 1,
                    "global_app_rules": ["Ship a smaller finished app."],
                    "phases": {
                        "tech_specs": {
                            "rules": ["Choose simple architecture."],
                            "required_output": ["Data models."],
                            "acceptance_checks": ["Workers can start."]
                        }
                    }
                }, fh)
            out = pr.render_phase_playbook(d, "app", "tech_specs")
        self.assertIn("PHASE PLAYBOOK", out)
        self.assertIn("Ship a smaller finished app", out)
        self.assertIn("Choose simple architecture", out)
        self.assertIn("Workers can start", out)

    def test_render_phase_quality_rubric(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, pr.RULES_FILENAME), "w", encoding="utf-8") as fh:
                json.dump({
                    "schema_version": 1,
                    "global_app_rules": ["Ship a smaller finished app."],
                    "phases": {
                        "app_features": {
                            "rules": ["Prioritize ruthlessly."],
                            "required_output": ["Acceptance criteria per must-have."],
                            "acceptance_checks": ["Build phase can complete it."]
                        }
                    }
                }, fh)
            out = pr.render_phase_quality_rubric(d, "app", "app_features")
        self.assertIn("Global quality bar", out)
        self.assertIn("Prioritize ruthlessly", out)
        self.assertIn("Acceptance criteria", out)
        self.assertIn("Build phase can complete", out)

    def test_build_context_includes_phase_playbook(self):
        phase = wf.Phase("tech_specs", "tech_specs", "tech_specs.md", "spec it")
        cfg = {"_phase_playbook": "===== PHASE PLAYBOOK =====\n- Pick SwiftUI.",
               "_workflow_target": "app"}
        ctx = orch.build_context(cfg, "demo", phase, "Build an iOS app", [], "")
        self.assertIn("PHASE PLAYBOOK", ctx)
        self.assertIn("Pick SwiftUI", ctx)

    def test_build_context_injects_phase_exemplar(self):
        # process_phase sets cfg["_phase_exemplar"] from
        # orch._load_phase_exemplar(key) before build_context runs — this is
        # the actual splice point _load_phase_exemplar's output reaches.
        phase = wf.Phase("app_features", "app_features", "app_features.md", "list features")
        cfg = {"_phase_exemplar": "\n===== EXEMPLAR — a previous run's output "
                                  "=====\nPrioritized feature list goes here.",
               "_workflow_target": "app"}
        ctx = orch.build_context(cfg, "demo", phase, "Build an iOS app", [], "")
        self.assertIn("EXEMPLAR", ctx)
        self.assertIn("Prioritized feature list goes here.", ctx)

    def test_build_context_omits_exemplar_block_when_none_loaded(self):
        phase = wf.Phase("app_features", "app_features", "app_features.md", "list features")
        cfg = {"_workflow_target": "app"}
        ctx = orch.build_context(cfg, "demo", phase, "Build an iOS app", [], "")
        self.assertNotIn("EXEMPLAR", ctx)

    def test_app_build_has_quality_gate_spine(self):
        phases = [p.key for p in wf.load_workflow("app_build").phases]
        self.assertEqual(phases[0], "prompt_contract")
        self.assertLess(phases.index("product_research"), phases.index("initial_discussion"))
        for key in (
            "prompt_contract",
            "portfolio_selection",
            "per_app_product_brief",
            "design_handoff",
            "ios_architecture_review",
            "implementation_readiness_gate",
            "build_verification",
            "human_qa_checklist",
            "app_store_readiness",
            "portfolio_audit",
        ):
            self.assertIn(key, phases)
        self.assertLess(phases.index("build_coordination"),
                        phases.index("build_verification"))


if __name__ == "__main__":
    unittest.main()


class TestLayeredRules(unittest.TestCase):
    """V3 board 3.2: section -> project-override -> global, whole-entry
    precedence, silent-absence, corrupt-layer banner, per-layer cache."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._write(os.path.join(self.d, pr.RULES_FILENAME), {
            "schema_version": 1,
            "global_app_rules": ["global bar"],
            "phases": {"report": {"rules": ["global report rule"]},
                       "only_global": {"rules": ["untouched"]}}})

    def _write(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(obj, str):
                fh.write(obj)
            else:
                json.dump(obj, fh)
        # distinct mtime_ns per write on coarse filesystems
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))

    def _layer(self, name, phases):
        path = os.path.join(self.d, "layers", name)
        self._write(path, {"schema_version": 1, "phases": phases})
        return path

    def test_collision_two_sections_each_resolve_their_own(self):
        research = self._layer("research.json",
                               {"report": {"rules": ["research style"]}})
        audit = self._layer("audit.json",
                            {"report": {"rules": ["audit style"]}})
        a = pr.render_phase_playbook(self.d, "app", "report", layers=[research])
        b = pr.render_phase_playbook(self.d, "app", "report", layers=[audit])
        self.assertIn("research style", a)
        self.assertNotIn("audit style", a)
        self.assertIn("audit style", b)

    def test_three_layer_precedence_whole_entry(self):
        section = self._layer("section.json",
                              {"report": {"rules": ["section wins"]}})
        project = self._layer("project.json",
                              {"report": {"rules": ["project wins"],
                                          "required_output": ["proj out"]},
                               "other": {"rules": ["project other"]}})
        rules = pr.load_rules_layered(self.d, [section, project])
        # section supplies the WHOLE report entry — project's
        # required_output must NOT merge in.
        self.assertEqual(rules["phases"]["report"],
                         {"rules": ["section wins"]})
        self.assertEqual(rules["phases"]["other"]["rules"], ["project other"])
        self.assertEqual(rules["phases"]["only_global"]["rules"], ["untouched"])
        # project beats global when no section layer has the key
        rules2 = pr.load_rules_layered(self.d, [project])
        self.assertEqual(rules2["phases"]["report"]["rules"],
                         ["project wins"])

    def test_global_app_rules_never_read_from_a_layer(self):
        path = os.path.join(self.d, "layers", "s.json")
        self._write(path, {"schema_version": 1,
                           "global_app_rules": ["layer global MUST NOT APPLY"],
                           "phases": {"report": {"rules": ["x"]}}})
        out = pr.render_phase_playbook(self.d, "app", "report", layers=[path])
        self.assertIn("global bar", out)
        self.assertNotIn("MUST NOT APPLY", out)

    def test_absent_layer_is_silent_corrupt_layer_banners_once(self):
        events = []
        missing = os.path.join(self.d, "layers", "nope.json")
        pr.render_phase_playbook(self.d, "app", "report", layers=[missing],
                                 on_fallback=lambda p, e: events.append(p))
        self.assertEqual(events, [], "absence is normal, never a banner")
        corrupt = os.path.join(self.d, "layers", "bad.json")
        self._write(corrupt, "{broken")
        out = pr.render_phase_playbook(self.d, "app", "report",
                                       layers=[corrupt],
                                       on_fallback=lambda p, e: events.append(p))
        self.assertEqual(events, [corrupt], "corrupt banners exactly once")
        self.assertIn("global report rule", out, "falls through to global")
        # cached failure: a second render neither re-parses nor re-banners
        pr.render_phase_playbook(self.d, "app", "report", layers=[corrupt],
                                 on_fallback=lambda p, e: events.append(p))
        self.assertEqual(len(events), 1)

    def test_cache_busts_on_layer_edit(self):
        layer = self._layer("live.json", {"report": {"rules": ["v1"]}})
        self.assertIn("v1", pr.render_phase_playbook(
            self.d, "app", "report", layers=[layer]))
        self._write(layer, {"schema_version": 1,
                            "phases": {"report": {"rules": ["v2"]}}})
        self.assertIn("v2", pr.render_phase_playbook(
            self.d, "app", "report", layers=[layer]))

    def test_flat_run_parity_no_layers(self):
        # Byte parity: with no layers the rendered text is IDENTICAL to
        # the pre-3.2 path (asserted, not claimed).
        self.assertEqual(
            pr.render_phase_playbook(self.d, "app", "report"),
            pr.render_phase_playbook(self.d, "app", "report", layers=[]))
        self.assertEqual(
            pr.render_phase_quality_rubric(self.d, "app", "report"),
            pr.render_phase_quality_rubric(self.d, "app", "report",
                                           layers=None))

    def test_orchestrator_layer_derivation(self):
        cfg = {"_app_dir": os.path.join(self.d, "proj", "ideas", "chat"),
               "root": self.d}
        layers = orch._phase_rule_layers(cfg)
        self.assertEqual(len(layers), 2)
        self.assertTrue(layers[0].endswith(
            os.path.join("sections", "ideas", "rules.json")))
        self.assertTrue(layers[1].endswith(
            os.path.join("proj", pr.RULES_FILENAME)))
        flat = {"_app_dir": os.path.join(self.d, "proj"), "root": self.d}
        self.assertEqual(orch._phase_rule_layers(flat), [],
                         "flat runs get no layers — byte-identical path")
