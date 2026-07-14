"""Per-iteration verification + evidence-backed consensus in _run_parallel_build
(runtime.verify_between_iterations). Agents and the verifier are stubbed — same
harness pattern as tests/test_worktree_integration.py."""
import os
import subprocess
import tempfile
import unittest
import unittest.mock

import mistakes as mk
import orchestrator as orch
import workflows as wf


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


VERIFY_OK = {"ran": True, "ok": True, "tool": "shell", "summary": "compiled", "errors": ""}
VERIFY_FAIL = {"ran": True, "ok": False, "tool": "shell",
               "summary": "compile FAILED", "errors": "error: missing semicolon"}
VERIFY_SKIP = {"ran": False, "ok": False, "tool": "shell",
               "summary": "no toolchain", "errors": ""}


@unittest.skipUnless(_has_git(), "git not available")
class IterationVerifyHarness(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        self.md_path = os.path.join(self.app_dir, "build_coordination.md")
        with open(self.md_path, "w") as f:
            f.write("")
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig_call = orch.call_agent
        self._orig_available = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent in ("codex", "claude")
        self.prompts = []          # (rnd, agent, prompt)
        self.consensus_script = {}  # iteration -> "YES"/"NO" (default YES)

        def fake(cfg, app, phase, rnd, agent, prompt):
            self.prompts.append((str(rnd), agent, prompt))
            bd = cfg.get("_build_dir")
            if "integrate" in str(rnd):
                it = int(str(rnd).split(".")[0])
                verdict = self.consensus_script.get(it, "YES")
                return "integrated. CONSENSUS: %s" % verdict
            with open(os.path.join(bd, "%s_lane_%s.swift" % (agent, rnd)), "w") as fh:
                fh.write("// by %s\n" % agent)
            return "wrote %s lane" % agent
        orch.call_agent = fake

    def tearDown(self):
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_available

    def _cfg(self, verify_between=True):
        return {
            "agents": {"codex_enabled": True, "claude_enabled": True,
                       "gemini_enabled": False},
            "runtime": {"worktree_isolation": False, "build_parallel_workers": 2,
                        "build_cross_review": False,
                        "verify_between_iterations": verify_between},
            "_allow_writes": True, "_build_dir": self.build_dir, "root": self.app_dir,
            "_workflow_target": "app", "_workflow_name": "vslice",
        }

    def _phase(self):
        return wf.Phase("build_coordination", ".", "build_coordination.md",
                        "build it", rounds=1, writes=True)

    def _run(self, verify_results, max_rounds=1, verify_between=True):
        """verify_results: list of dicts run_verification returns per call."""
        calls = []

        def fake_verify(build_dir, spec, timeout=1200):
            calls.append(dict(spec=spec, timeout=timeout))
            idx = min(len(calls) - 1, len(verify_results) - 1)
            return dict(verify_results[idx])

        cfg = self._cfg(verify_between=verify_between)
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        with unittest.mock.patch.object(orch.verifylib, "run_verification",
                                        fake_verify):
            consensus, out, transcript = orch._run_parallel_build(
                cfg, "demo", self.app_dir, self._phase(), "build a tiny app", [],
                state, self.md_path, max_rounds, "", "")
        return consensus, transcript, calls


class TestEvidenceBackedConsensus(IterationVerifyHarness):
    def test_consensus_accepted_when_verify_passes(self):
        consensus, _tr, calls = self._run([VERIFY_OK])
        self.assertTrue(consensus)
        self.assertEqual(len(calls), 1)

    def test_consensus_rejected_when_verify_fails_then_loop_continues(self):
        consensus, transcript, calls = self._run([VERIFY_FAIL], max_rounds=2)
        # Both iterations ran (consensus rejected at 1, still failing at 2).
        self.assertFalse(consensus)
        self.assertEqual(len(calls), 2)
        self.assertIn("declared consensus but the build fails to compile",
                      transcript)
        # The compile errors were fed back into the shared context.
        self.assertIn("error: missing semicolon", transcript)
        # And the second iteration's worker prompts carried them.
        iter2_worker = [p for (rnd, a, p) in self.prompts
                        if rnd.startswith("2.") and "integrate" not in rnd]
        self.assertTrue(iter2_worker)
        self.assertTrue(any("missing semicolon" in p for p in iter2_worker))
        # Ledger: consensus_unverified recorded.
        classes = [r["cls"] for r in mk.load_mistakes(self.app_dir)]
        self.assertIn("consensus_unverified", classes)
        self.assertIn("verify_failure", classes)

    def test_consensus_recovers_when_a_later_iteration_compiles(self):
        consensus, _tr, calls = self._run([VERIFY_FAIL, VERIFY_OK], max_rounds=3)
        self.assertTrue(consensus)
        self.assertEqual(len(calls), 2)   # accepted at iteration 2, loop ended

    def test_unverified_never_blocks_consensus(self):
        consensus, transcript, calls = self._run([VERIFY_SKIP])
        self.assertTrue(consensus)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fails to compile", transcript)

    def test_knob_off_restores_legacy_behavior(self):
        consensus, _tr, calls = self._run([VERIFY_FAIL], verify_between=False)
        self.assertTrue(consensus)          # legacy: free-text consensus wins
        self.assertEqual(calls, [])          # verifier never invoked

    def test_toolchain_absent_cached_verify_called_once(self):
        # Integrator says NO at iteration 1, YES at 2: two iterations run, but
        # after the first ran=false result the verifier is not re-invoked.
        self.consensus_script = {1: "NO", 2: "YES"}
        consensus, _tr, calls = self._run([VERIFY_SKIP], max_rounds=3)
        self.assertTrue(consensus)
        self.assertEqual(len(calls), 1)

    def test_workflow_verify_spec_used_when_phase_has_none(self):
        calls = []

        def fake_verify(build_dir, spec, timeout=1200):
            calls.append(spec)
            return dict(VERIFY_OK)

        cfg = self._cfg()
        cfg["_workflow_verify_spec"] = {"type": "shell", "command": "make"}
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        with unittest.mock.patch.object(orch.verifylib, "run_verification",
                                        fake_verify):
            orch._run_parallel_build(cfg, "demo", self.app_dir, self._phase(),
                                     "p", [], state, self.md_path, 1, "", "")
        self.assertEqual(calls, [{"type": "shell", "command": "make"}])

    def test_iteration_results_persisted(self):
        import verify as verifylib
        self._run([VERIFY_FAIL, VERIFY_OK], max_rounds=3)
        recs = verifylib.load_verify_results(self.app_dir)
        self.assertEqual([r["status"] for r in recs], ["failed", "verified"])
        self.assertTrue(all(r["phase"] == "build_coordination" for r in recs))


class TestContractErrorWarn(unittest.TestCase):
    """Feature 2c: contract errors WARN prominently + ledger, never hard-block
    (exercises the real post-phase recorder, _record_phase_contracts)."""

    def test_tasks_errors_warn_and_ledger(self):
        with tempfile.TemporaryDirectory() as app_dir:
            msgs = []
            # Malformed task (missing required fields) + a dependency cycle.
            blob = ('```tasks-json\n{"tasks": ['
                    '{"id": "T-1"},'
                    '{"id": "T-2", "title": "a", "owner_lane": "data_domain",'
                    ' "files": [], "status": "pending", "depends_on": ["T-3"]},'
                    '{"id": "T-3", "title": "b", "owner_lane": "primary_ui",'
                    ' "files": [], "status": "pending", "depends_on": ["T-2"]}'
                    ']}\n```')
            with unittest.mock.patch.object(orch, "emit", msgs.append):
                orch._record_phase_contracts({}, "demo", app_dir,
                                             "task_assignments", blob, "")
            self.assertTrue(any("WARN CONTRACT" in m for m in msgs))
            self.assertTrue(any("dependency cycle" in m for m in msgs))
            recs = mk.load_mistakes(app_dir)
            self.assertEqual(recs[0]["cls"], "contract_error")
            self.assertIn("tasks.json", recs[0]["summary"])
            # Non-fatal: the valid tasks still persisted alongside the errors.
            self.assertEqual(len(orch.load_tasks(app_dir)), 2)

    def test_interface_errors_warn_and_ledger(self):
        with tempfile.TemporaryDirectory() as app_dir:
            msgs = []
            blob = '```interfaces-json\n{"interfaces": [{"name": "Foo"}]}\n```'
            with unittest.mock.patch.object(orch, "emit", msgs.append):
                orch._record_phase_contracts({}, "demo", app_dir,
                                             "tech_specs", blob, "")
            self.assertTrue(any("WARN CONTRACT" in m for m in msgs))
            self.assertEqual(mk.load_mistakes(app_dir)[0]["cls"], "contract_error")

    def test_clean_contract_neither_warns_nor_ledgers(self):
        with tempfile.TemporaryDirectory() as app_dir:
            msgs = []
            blob = ('```tasks-json\n{"tasks": [{"id": "T-1", "title": "a", '
                    '"owner_lane": "data_domain", "files": [], '
                    '"status": "pending"}]}\n```')
            with unittest.mock.patch.object(orch, "emit", msgs.append):
                orch._record_phase_contracts({}, "demo", app_dir,
                                             "task_assignments", blob, "")
            self.assertFalse(any("WARN CONTRACT" in m for m in msgs))
            self.assertEqual(mk.load_mistakes(app_dir), [])


if __name__ == "__main__":
    unittest.main()
