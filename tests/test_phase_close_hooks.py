"""V3 board 2.2c: the phase-close hook sequence — order pin + end-to-end
wiring for the hooks with no prior coverage (flows/requirements, audit
render, library-mining report), plus the sprint-handoff direct call.

Wiring tests drive process_phase (never monkeypatch hook names — the tuple
freezes references at import)."""
import json
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


class HookBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda a, cfg=None: a == "codex"

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch._agent_available = self._orig_avail

    def _cfg(self, **extra):
        cfg = {"agents": {"codex_enabled": True, "claude_enabled": False,
                          "gemini_enabled": False},
               "runtime": {"parallel_discussion_rounds": False,
                           "phase_quality_gates_enabled": False,
                           "phase_independent_first_round_enabled": False},
               "_workflow_target": "app", "_app_dir": self.app_dir,
               "root": self.app_dir}
        cfg.update(extra)
        return cfg

    def _state(self):
        return {"current_phase": None, "current_round": 0,
                "completed_phases": [], "phase_outputs": {},
                "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}

    def _consensus_stub(self, final_body):
        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            if (session_key or "").endswith(":coord"):
                return ("Agreed.\n\nCONSENSUS: YES\n\n## Final Output\n\n%s\n"
                        % final_body)
            return "position with %s" % final_body
        return sessioned


class TestHookOrderPin(unittest.TestCase):
    def test_tuple_order_is_the_contract(self):
        # Deliberately a change-detector: verify before signing (device
        # settings enforced last), contracts after verify (transcript must
        # include repair rounds), audit before the verification label (audit
        # REPLACES final_output; the label APPENDS), artifact publication
        # LAST (it reads the exact recorded output — mutators before
        # readers; its events must precede phase_completed).
        self.assertEqual(
            [h.__name__ for h in orch._PHASE_CLOSE_HOOKS],
            ["_hook_sprint_verify_reserve", "_hook_verify_repair",
             "_hook_ios_signing", "_hook_secret_scan",
             "_hook_record_contracts", "_hook_flows_requirements_research",
             "_hook_compliance_report", "_hook_document_provenance",
             "_hook_library_mining",
             "_hook_audit_report",
             "_hook_verification_label", "_hook_artifact_publish"])


class TestHookWiring(HookBase):
    def test_flows_json_persisted_from_task_assignments(self):
        body = ('```flows-json\n{"flows": [{"name": "open_app", "steps": '
                '[{"tap": "startButton"}]}]}\n```')
        orch.call_agent_sessioned = self._consensus_stub(body)
        orch.process_phase(self._cfg(), "hooks", self.app_dir,
                           wf.Phase("task_assignments", ".", "t.md", "plan",
                                    rounds=1),
                           "p", [], self._state())
        with open(os.path.join(self.app_dir, "flows.json"),
                  encoding="utf-8") as fh:
            flows = json.load(fh)
        self.assertTrue(flows.get("flows"))

    def test_requirements_json_persisted_from_app_features(self):
        body = ('```requirements-json\n{"requirements": [{"id": "R1", '
                '"text": "works offline", "priority": "core"}]}\n```')
        orch.call_agent_sessioned = self._consensus_stub(body)
        orch.process_phase(self._cfg(), "hooks", self.app_dir,
                           wf.Phase("app_features", ".", "f.md", "features",
                                    rounds=1),
                           "p", [], self._state())
        with open(os.path.join(self.app_dir, "requirements.json"),
                  encoding="utf-8") as fh:
            reqs = json.load(fh)
        self.assertTrue(reqs.get("requirements"))

    def test_audit_report_replaces_final_output(self):
        body = ('```finding-json\n{"title": "SQL injection in login", '
                '"severity": "high", "file": "auth.py", "line": 10, '
                '"detail": "unparameterized query"}\n```')
        orch.call_agent_sessioned = self._consensus_stub(body)
        out = orch.process_phase(self._cfg(_workflow_target="audit"),
                                 "hooks", self.app_dir,
                                 wf.Phase("report", ".", "r.md", "report",
                                          rounds=1),
                                 "p", [], self._state())
        self.assertTrue(os.path.exists(
            os.path.join(self.app_dir, "report", "findings.json")))
        self.assertTrue(os.path.exists(
            os.path.join(self.app_dir, "report", "AUDIT_REPORT.md")))
        # REPLACE semantics: the returned output IS the rendered report.
        self.assertIn("Audit Report", out)

    def test_library_mining_report_written(self):
        orch.call_agent_sessioned = self._consensus_stub(
            "Extraction plan: one reusable networking layer.")
        orch.process_phase(self._cfg(_workflow_target="library_mining"),
                           "hooks", self.app_dir,
                           wf.Phase("extraction_candidates", ".", "e.md",
                                    "mine", rounds=1),
                           "p", [], self._state())
        with open(os.path.join(self.app_dir, "report", "LIBRARY_REPORT.md"),
                  encoding="utf-8") as fh:
            self.assertIn("Extraction Report", fh.read())

    def test_sprint_handoff_direct_call(self):
        cfg = self._cfg(_budget={"time_budget_minutes": 1},
                        _deadline=12345.0, _phase_deadline=1.0)
        t, f = orch._hook_sprint_verify_reserve(
            cfg, "hooks", self.app_dir,
            wf.Phase("x", ".", "x.md", "p"), self._state(),
            key="x", md_path=os.path.join(self.app_dir, "x.md"),
            transcript="T", final_output="F", coord="codex",
            active=["codex"], is_build=False, is_verify_repair=False,
            allow_writes=False, _needs_vlabel=False)
        self.assertEqual((t, f), ("T", "F"))
        self.assertEqual(cfg["_phase_deadline"], 12345.0)
        # Without a budget: untouched.
        cfg2 = self._cfg(_phase_deadline=7.0)
        orch._hook_sprint_verify_reserve(
            cfg2, "hooks", self.app_dir,
            wf.Phase("x", ".", "x.md", "p"), self._state(),
            key="x", md_path=os.path.join(self.app_dir, "x.md"),
            transcript="T", final_output="F", coord="codex",
            active=["codex"], is_build=False, is_verify_repair=False,
            allow_writes=False, _needs_vlabel=False)
        self.assertEqual(cfg2["_phase_deadline"], 7.0)


if __name__ == "__main__":
    unittest.main()
