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
