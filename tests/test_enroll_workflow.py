"""Enroll workflow and immutable-origin engine plumbing (E2)."""

import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock

import enroll
import orchestrator as orch
import workflows


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _tree_hash(root):
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode("utf-8"))
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


class EnrollWorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="enroll-e2-")
        self.root = os.path.join(self.base, "workspace")
        self.origin = os.path.join(self.base, "origin")
        os.makedirs(self.origin)
        _write(os.path.join(self.origin, "Package.swift"), "// swift-tools-version: 5.9\n")
        _write(os.path.join(self.origin, "Sources", "App.swift"), "struct App {}\n")
        self.result = enroll.scaffold(self.root, self.origin, name="adopted")
        self.app_dir = self.result["app_dir"]
        with open(os.path.join(self.app_dir, "initial_prompt", "initial_prompt.md"),
                  encoding="utf-8") as fh:
            self.prompt = fh.read()
        self.old_quiet = orch._QUIET
        orch._QUIET = True

    def tearDown(self):
        orch._QUIET = self.old_quiet
        shutil.rmtree(self.base, ignore_errors=True)

    def cfg(self, **extra):
        cfg = {
            "root": self.root,
            "_app_dir": self.app_dir,
            "_workflow_target": "enroll",
            "_target_path": os.path.realpath(self.origin),
            "_target_paths": [],
            "agents": {"codex_enabled": True, "claude_enabled": False,
                       "gemini_enabled": False, "ollama_enabled": False},
            "runtime": {"parallel_discussion_rounds": False,
                        "phase_quality_gates_enabled": False,
                        "phase_independent_first_round_enabled": False,
                        "build_code_changes_enabled": True,
                        "audit_live_read_cwd": True,
                        "fleet_ledger_enabled": False},
        }
        cfg.update(extra)
        return cfg

    @staticmethod
    def state():
        return {"current_phase": None, "current_round": 0,
                "completed_phases": [], "phase_outputs": {},
                "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}

    @staticmethod
    def consensus_stub(side_effect=None, captured=None):
        def run(cfg, app, phase, rnd, agent, prompt,
                delta_prompt=None, session_key=None):
            if captured is not None:
                captured.append((dict(cfg), prompt))
            if side_effect is not None:
                side_effect(cfg)
            if (session_key or "").endswith(":coord"):
                return "CONSENSUS: YES\n\n## Final Output\nObserved output."
            return "Observed position."
        return run


class TestEnrollWorkflowDefinition(EnrollWorkflowFixture):
    def test_exact_phase_order_is_terminal_and_read_only(self):
        workflow = workflows.load_workflow("enroll", HERE)
        self.assertEqual(workflow.target, "enroll")
        self.assertIsNone(workflow.build_phase)
        self.assertEqual([phase.key for phase in workflow.phases], [
            "recon_understand", "docs_inventory", "compliance_check",
            "doc_rebuild", "enroll_report"])
        self.assertTrue(all(phase.reads_target for phase in workflow.phases))
        self.assertTrue(all(not phase.writes for phase in workflow.phases))
        self.assertEqual(workflow.phases[-1].key, "enroll_report")

    def test_missing_target_records_clean_hard_error_before_any_phase(self):
        os.remove(os.path.join(self.app_dir, "target_path.txt"))
        with mock.patch.object(orch, "process_phase",
                               side_effect=AssertionError("phase must not start")):
            orch.process_app({"root": self.root, "runtime": {}},
                             self.root, "adopted")
        state = orch.load_state(self.app_dir)
        self.assertIn("enroll workflow needs", state["error"])
        self.assertFalse(state["done"])

    def test_ios_knowledge_is_injected_through_the_real_phase_path(self):
        workflow = workflows.load_workflow("enroll", HERE)
        captured = []
        domains = []

        def retrieve(_here, domain, _query, **_kwargs):
            domains.append(domain)
            return "IOS-KNOWLEDGE-EVIDENCE"

        with mock.patch.object(orch, "_agent_available",
                               side_effect=lambda agent, cfg=None: agent == "codex"), \
                mock.patch.object(orch.knowlib, "retrieve", side_effect=retrieve), \
                mock.patch.object(orch, "call_agent_sessioned",
                                  side_effect=self.consensus_stub(captured=captured)):
            for index, phase in enumerate(workflow.phases):
                orch.process_phase(self.cfg(), "adopted", self.app_dir, phase,
                                   self.prompt, [], self.state(), phase_index=index)
        self.assertEqual(domains, ["ios"] * len(workflow.phases))
        self.assertTrue(captured)
        self.assertTrue(all("IOS-KNOWLEDGE-EVIDENCE" in prompt
                            for _cfg, prompt in captured))
        self.assertTrue(all(call_cfg.get("_target_path") ==
                            os.path.realpath(self.origin)
                            for call_cfg, _prompt in captured))

    def test_pipeline_threads_target_path_to_every_phase(self):
        seen = []

        def phase_stub(cfg, app, app_dir, phase, prompt, prior, state,
                       phase_index=0):
            seen.append((phase.key, cfg.get("_target_path"),
                         cfg.get("_workflow_target")))
            state.setdefault("completed_phases", []).append(phase.key)
            state.setdefault("phase_outputs", {})[phase.key] = "observed"
            orch.save_state(app_dir, state)
            return "observed"

        cfg = {"root": self.root,
               "runtime": {"fetch_prompt_urls": False,
                           "fleet_ledger_enabled": False,
                           "docs_git_sync_enabled": False}}
        with mock.patch.object(orch, "process_phase", side_effect=phase_stub), \
                mock.patch.object(orch, "_release_gate_failure", return_value=None), \
                mock.patch.object(orch, "_adherence_gate", return_value=None), \
                mock.patch.object(orch.docslib, "write_project_docs", return_value=[]), \
                mock.patch.object(orch.docslib, "write_project_archive", return_value=[]):
            orch.process_app(cfg, self.root, "adopted")
        self.assertEqual([key for key, _target, _kind in seen], [
            "recon_understand", "docs_inventory", "compliance_check",
            "doc_rebuild", "enroll_report"])
        self.assertTrue(all(target == os.path.realpath(self.origin)
                            for _key, target, _kind in seen))
        self.assertTrue(all(kind == "enroll" for _key, _target, kind in seen))

    def test_sabotage_runner_write_never_receives_origin_as_writable_cwd(self):
        """Board sabotage test: the fake runner writes in the cwd it receives.

        Even with audit_live_read_cwd enabled and a malicious writes=true phase,
        enroll must force a read-only turn in an isolated cwd. The origin's
        content hash and file set remain exact.
        """
        before = _tree_hash(self.origin)
        attempted_cwds = []

        def attempt_write(cfg):
            self.assertFalse(cfg.get("_allow_writes"))
            self.assertIsNone(cfg.get("_build_dir"))
            cwd, ephemeral = orch._agent_cwd(cfg)
            attempted_cwds.append(os.path.realpath(cwd))
            try:
                _write(os.path.join(cwd, "ENROLL_WRITE_ATTEMPT"), "attempted")
            finally:
                if ephemeral:
                    shutil.rmtree(cwd, ignore_errors=True)

        malicious = workflows.Phase(
            "recon_understand", "recon", "recon.md", "inspect Swift",
            rounds=1, roles=["backend"], writes=True, reads_target=True)
        with mock.patch.object(orch, "_agent_available",
                               side_effect=lambda agent, cfg=None: agent == "codex"), \
                mock.patch.object(orch, "call_agent_sessioned",
                                  side_effect=self.consensus_stub(attempt_write)):
            orch.process_phase(self.cfg(), "adopted", self.app_dir, malicious,
                               self.prompt, [], self.state())
        self.assertTrue(attempted_cwds)
        self.assertTrue(all(cwd != os.path.realpath(self.origin)
                            for cwd in attempted_cwds))
        self.assertEqual(_tree_hash(self.origin), before)
        self.assertFalse(os.path.exists(os.path.join(self.origin,
                                                      "ENROLL_WRITE_ATTEMPT")))
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, "app_build")))


if __name__ == "__main__":
    unittest.main()
