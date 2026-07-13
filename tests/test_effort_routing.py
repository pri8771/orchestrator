"""Effort-routing extensions: gemini/ollama reasoning fields (accepted-but-noop,
evidence-based) and per-role (worker vs integrator) overrides within a phase."""
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock

import modelrouting as mr
import orchestrator as orch
import workflows as wf


def write_routing(d, payload):
    path = os.path.join(d, mr.ROUTING_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


class TestNewPhaseFields(unittest.TestCase):
    def test_gemini_and_ollama_reasoning_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {"phases": {"build_coordination":
                                         {"gemini_reasoning": "low",
                                          "ollama_reasoning": "high"}}})
            r = mr.load_routing(d)
        ov = mr.overrides_for(r, "build_coordination")
        self.assertEqual(ov["gemini_reasoning"], "low")
        self.assertEqual(ov["ollama_reasoning"], "high")

    def test_noop_fields_warn_once_and_do_not_change_models(self):
        cfg = {"models": {}, "_resolved": {"gemini_model": "", "ollama_model": ""},
               "_routing": {"enabled": True, "fallback": {},
                            "phases": {"tech_specs": {"gemini_reasoning": "low",
                                                      "ollama_reasoning": "high"}}}}
        msgs = []
        with unittest.mock.patch.object(orch, "emit", msgs.append):
            c = orch._apply_phase_routing(cfg, "tech_specs")
            orch._apply_phase_routing(cfg, "tech_specs")   # second pass: no re-warn
        warns = [m for m in msgs if "accepted but ignored" in m]
        self.assertEqual(len(warns), 2)   # one per field, once total
        self.assertTrue(any("gemini_reasoning" in m for m in warns))
        self.assertTrue(any("ollama_reasoning" in m for m in warns))
        # No invented flag/model mutation from the noop fields.
        self.assertEqual(c["_resolved"]["gemini_model"], "")
        self.assertEqual(c["_resolved"]["ollama_model"], "")
        self.assertNotIn("gemini_reasoning", c["models"])
        self.assertNotIn("ollama_reasoning", c["models"])


class TestRolesParsing(unittest.TestCase):
    def test_roles_parsed_and_validated(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {"phases": {"build_coordination": {
                "codex_reasoning": "medium",
                "roles": {
                    "worker": {"codex_reasoning": "low", "evil": "x",
                               "codex": "bad\nvalue"},
                    "integrator": {"claude_reasoning": "high",
                                   "claude": "claude-opus-4-8"},
                    "reviewer": {"codex_reasoning": "high"},   # unknown role
                    "empty": "not a dict",
                }}}})
            warned = []
            r = mr.load_routing(d, on_warn=warned.append)
        roles = mr.overrides_for(r, "build_coordination")["roles"]
        self.assertEqual(roles["worker"], {"codex_reasoning": "low"})
        self.assertEqual(roles["integrator"], {"claude_reasoning": "high",
                                               "claude": "claude-opus-4-8"})
        self.assertNotIn("reviewer", roles)
        self.assertNotIn("empty", roles)
        self.assertTrue(any("unknown role" in w and "reviewer" in w for w in warned))

    def test_roles_absent_is_default(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {"phases": {"tech_specs": {"codex": "gpt-5.4"}}})
            r = mr.load_routing(d)
        self.assertNotIn("roles", mr.overrides_for(r, "tech_specs"))


class TestApplyRoleRouting(unittest.TestCase):
    def _phase_cfg(self):
        cfg = {"models": {"codex_reasoning": "medium",
                          "codex_build_reasoning": "medium"},
               "_resolved": {"claude_model": "claude-sonnet-5",
                             "codex_model": "gpt-5.4"},
               "_routing": {"enabled": True, "fallback": {},
                            "phases": {"build_coordination": {
                                "roles": {"worker": {"codex_reasoning": "low"},
                                          "integrator": {"claude_reasoning": "high",
                                                         "claude": "claude-opus-4-8"}}}}}}
        return orch._apply_phase_routing(cfg, "build_coordination")

    def test_worker_and_integrator_get_different_efforts(self):
        c = self._phase_cfg()
        wcfg = orch._apply_role_routing(dict(c), "worker")
        icfg = orch._apply_role_routing(dict(c), "integrator")
        self.assertEqual(wcfg["models"]["codex_build_reasoning"], "low")
        self.assertEqual(wcfg["models"]["codex_reasoning"], "low")
        self.assertNotIn("claude_reasoning", wcfg["models"])
        self.assertEqual(icfg["models"]["claude_build_reasoning"], "high")
        self.assertEqual(icfg["_resolved"]["claude_model"], "claude-opus-4-8")
        # phase-level cfg untouched by either role application
        self.assertEqual(c["models"]["codex_build_reasoning"], "medium")
        self.assertEqual(c["_resolved"]["claude_model"], "claude-sonnet-5")

    def test_no_role_block_is_a_noop(self):
        cfg = {"models": {"codex_reasoning": "medium"}, "_resolved": {}}
        out = orch._apply_role_routing(dict(cfg), "worker")
        self.assertEqual(out["models"], cfg["models"])


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git not available")
class TestRoleRoutingEndToEnd(unittest.TestCase):
    """Through the real _run_parallel_build: worker turns see the worker
    effort, the integrator turn sees the integrator effort."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        self.md_path = os.path.join(self.app_dir, "build.md")
        open(self.md_path, "w").close()
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig_call = orch.call_agent
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent in ("codex", "claude")
        self.seen = []   # (rnd, agent, codex_effort, claude_effort)

        def fake(cfg, app, phase, rnd, agent, prompt):
            self.seen.append((str(rnd), agent,
                              orch.cget(cfg, "models.codex_build_reasoning", None),
                              orch.cget(cfg, "models.claude_build_reasoning", None)))
            if "integrate" in str(rnd):
                return "integrated. CONSENSUS: YES"
            with open(os.path.join(cfg["_build_dir"], "%s.swift" % agent), "w") as fh:
                fh.write("//")
            return "built"
        orch.call_agent = fake

    def tearDown(self):
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_avail

    def test_worker_low_integrator_high(self):
        cfg = {"agents": {"codex_enabled": True, "claude_enabled": True,
                          "gemini_enabled": False},
               "runtime": {"build_parallel_workers": 2, "build_cross_review": False,
                           "verify_between_iterations": False},
               "models": {"codex_build_reasoning": "medium"},
               "_resolved": {"codex_model": "", "claude_model": ""},
               "_allow_writes": True, "_build_dir": self.build_dir,
               "root": self.app_dir, "_workflow_target": "app",
               "_app_dir": self.app_dir,
               "_routing": {"enabled": True, "fallback": {}, "phases": {
                   "build_coordination": {"roles": {
                       "worker": {"codex_reasoning": "low"},
                       "integrator": {"claude_reasoning": "high"}}}}}}
        c = orch._apply_phase_routing(cfg, "build_coordination")
        phase = wf.Phase("build_coordination", ".", "build.md", "b",
                         rounds=1, writes=True)
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        orch._run_parallel_build(c, "demo", self.app_dir, phase, "p", [],
                                 state, self.md_path, 1, "", "")
        workers = [s for s in self.seen if "integrate" not in s[0]]
        integ = [s for s in self.seen if "integrate" in s[0]]
        self.assertTrue(workers and integ)
        for _rnd, _agent, codex_e, claude_e in workers:
            self.assertEqual(codex_e, "low")
            self.assertIsNone(claude_e)
        for _rnd, _agent, codex_e, claude_e in integ:
            self.assertEqual(claude_e, "high")
            self.assertEqual(codex_e, "medium")   # integrator keeps phase default


if __name__ == "__main__":
    unittest.main()
