"""Enrollment E4: compliance contract, gate, and typed bus artifact."""

import json
import os
import shutil
import tempfile
import unittest

import artifacts
import compliance
import docs
import orchestrator as orch
import workflows


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ComplianceTests(unittest.TestCase):
    def setUp(self):
        self.target = tempfile.mkdtemp(prefix="enroll-compliance-target-")
        self.app = tempfile.mkdtemp(prefix="enroll-compliance-app-")
        self.addCleanup(shutil.rmtree, self.target, True)
        self.addCleanup(shutil.rmtree, self.app, True)
        with open(os.path.join(self.target, "Package.swift"), "w",
                  encoding="utf-8") as fh:
            fh.write("// observed project marker\n")

    def _findings(self):
        return [
            {"rule": rule, "verdict": "cannot-determine",
             "evidence_paths": ["Package.swift"],
             "why": "The file proves the project exists but does not contain "
                    "enough evidence to decide this rule."}
            for rule in compliance.ios_rule_areas(HERE)
        ]

    def _output(self, findings=None):
        return "```compliance-json\n%s\n```" % json.dumps(
            {"findings": self._findings() if findings is None else findings})

    def test_prompt_enumerates_every_ios_rule_area_and_exact_verdicts(self):
        prompt = compliance.prompt_contract(HERE)
        areas = compliance.ios_rule_areas(HERE)
        self.assertTrue(areas)
        for area in areas:
            self.assertEqual(prompt.count(area), 1, area)
        for verdict in compliance.VERDICTS:
            self.assertIn(verdict, prompt)
        self.assertIn("Never coerce cannot-determine", prompt)

    def test_artifact_schema_accepts_valid_and_rejects_missing_evidence(self):
        registry = artifacts.load_registry(HERE)
        errors = []
        invalid = artifacts.publish(
            self.app, "report body",
            {"type": "compliance_report", "title": "bad",
             "findings": [{"rule": "knowledge/ios/example.md",
                           "verdict": "compliant", "evidence_paths": [],
                           "why": "observed"}]},
            registry, consensus=True, on_error=errors.append)
        self.assertIsNone(invalid)
        self.assertTrue(any("evidence_paths" in error for error in errors))
        self.assertEqual(artifacts.list_artifacts(self.app), [])

        valid = artifacts.publish(
            self.app, "report body",
            {"type": "compliance_report", "title": "good",
             "findings": [{"rule": "knowledge/ios/example.md",
                           "verdict": "cannot-determine",
                           "evidence_paths": ["Package.swift"],
                           "why": "the available evidence is insufficient"}]},
            registry, consensus=True)
        self.assertIsNotNone(valid)
        self.assertEqual(artifacts.load_meta(self.app, valid)["status"], "final")

    def test_missing_evidence_is_rejected_by_real_phase_quality_gate(self):
        bad = self._findings()
        bad[0] = dict(bad[0], evidence_paths=[])
        phase = workflows.Phase("compliance_check", ".", "c.md", "audit")
        transcript_path = os.path.join(self.app, "c.md")
        open(transcript_path, "w", encoding="utf-8").close()
        called = []
        original = orch.call_agent
        orch.call_agent = lambda *args, **kwargs: called.append(args) or \
            "QUALITY: PASS\n## Feedback\nLooks fine."
        self.addCleanup(setattr, orch, "call_agent", original)
        passed, response, _transcript = orch.run_phase_quality_gate(
            {"_workflow_target": "enroll", "_target_path": self.target},
            "adopted", self.app, phase, 1, "codex", "context",
            self._output(bad), transcript_path, "", evaluator="codex")
        self.assertFalse(passed)
        self.assertIn("needs one or more evidence_paths", response)
        self.assertEqual(called, [],
                         "deterministic rejection cannot be overruled by a model")
        self.assertTrue(orch._phase_quality_gate_enabled(
            {"_workflow_target": "enroll",
             "runtime": {"phase_quality_gates_enabled": True}}, False))

    def test_hook_publishes_final_subscribe_ingestible_report_once(self):
        phase = workflows.Phase("compliance_check", ".", "c.md", "audit")
        kwargs = dict(key="compliance_check", md_path=None, transcript="",
                      final_output=self._output(), coord="codex", active=["codex"],
                      is_build=False, is_verify_repair=False, allow_writes=False,
                      _needs_vlabel=False, consensus=True)
        cfg = {"_workflow_target": "enroll", "_target_path": self.target}
        orch._hook_compliance_report(cfg, "adopted", self.app, phase, {}, **kwargs)
        orch._hook_compliance_report(cfg, "adopted", self.app, phase, {}, **kwargs)
        reports = artifacts.list_artifacts(self.app, type="compliance_report")
        self.assertEqual(len(reports), 1, "phase-close resume must not duplicate")
        meta = reports[0]
        self.assertEqual(meta["status"], "final")
        self.assertEqual(meta["doc_slots"], ["app_store_compliance"])
        self.assertEqual(meta["source"]["section"], "qa")
        self.assertEqual(meta["source"]["phase"], "compliance_check")
        self.assertTrue(all(f["verdict"] == "cannot-determine"
                            for f in meta["fields"]["findings"]))

        coverage = []
        docs.render_handoff_blueprint(
            "Adopted", docs._default_doc_map(), [], {}, self.app, artifacts,
            coverage=coverage)
        slot = next(row for row in coverage
                    if row["slot_id"] == "app_store_compliance")
        self.assertEqual(slot["status"], "filled")
        self.assertIn(meta["id"], slot["evidence"])

    def test_missing_or_unknown_rule_area_rejects_complete_report(self):
        findings = self._findings()[1:]
        findings.append({"rule": "knowledge/ios/invented.md",
                         "verdict": "not-applicable",
                         "evidence_paths": ["Package.swift"],
                         "why": "not an iOS rule"})
        report, errors = compliance.parse_output(
            self._output(findings), self.target, HERE)
        self.assertIsNone(report)
        self.assertTrue(any("missing rule area" in error for error in errors))
        self.assertTrue(any("unknown rule area" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
