"""Adaptive / quality-based escalation on repeated failure (item 1).

Covers: _apply_adaptive_escalation / _escalated_effort as pure functions,
_maybe_escalate's threshold + once-only logging, the verify/repair loop
wiring in _verify_and_repair, and the quality-gate retry wiring in
process_phase.
"""
import os
import tempfile
import unittest
import unittest.mock

import mistakes as mk
import orchestrator as orch
import workflows as wf


class TestEscalatedEffort(unittest.TestCase):
    def test_blank_bumps_to_high(self):
        self.assertEqual(orch._escalated_effort(""), "high")

    def test_low_bumps_to_high(self):
        self.assertEqual(orch._escalated_effort("low"), "high")

    def test_medium_bumps_to_high(self):
        self.assertEqual(orch._escalated_effort("medium"), "high")

    def test_high_bumps_to_max(self):
        self.assertEqual(orch._escalated_effort("high"), "max")

    def test_case_insensitive(self):
        self.assertEqual(orch._escalated_effort("HIGH"), "max")


class TestApplyAdaptiveEscalation(unittest.TestCase):
    def test_codex_bumps_both_reasoning_knobs(self):
        cfg = {"models": {"codex_reasoning": "low"}}
        ecfg = orch._apply_adaptive_escalation(cfg, "codex")
        self.assertEqual(ecfg["models"]["codex_reasoning"], "high")
        self.assertEqual(ecfg["models"]["codex_build_reasoning"], "high")
        # original cfg untouched
        self.assertEqual(cfg["models"]["codex_reasoning"], "low")

    def test_claude_bumps_both_reasoning_knobs(self):
        cfg = {"models": {"claude_reasoning": "high"}}
        ecfg = orch._apply_adaptive_escalation(cfg, "claude")
        self.assertEqual(ecfg["models"]["claude_reasoning"], "max")
        self.assertEqual(ecfg["models"]["claude_build_reasoning"], "max")

    def test_claude_model_override_applied_only_when_configured(self):
        cfg = {"models": {}, "runtime": {"escalation_model_override": "claude-opus-x"}}
        ecfg = orch._apply_adaptive_escalation(cfg, "claude")
        self.assertEqual(ecfg["_claude_model_override"], "claude-opus-x")

    def test_no_override_when_blank(self):
        cfg = {"models": {}, "runtime": {}}
        ecfg = orch._apply_adaptive_escalation(cfg, "claude")
        self.assertNotIn("_claude_model_override", ecfg)

    def test_gemini_is_a_cfg_noop(self):
        cfg = {"models": {"gemini_fallback": "gemini-3.5-flash"}}
        ecfg = orch._apply_adaptive_escalation(cfg, "gemini")
        self.assertEqual(ecfg["models"], cfg["models"])


class TestMaybeEscalate(unittest.TestCase):
    def test_below_threshold_no_escalation_no_log(self):
        with tempfile.TemporaryDirectory() as d:
            with unittest.mock.patch.object(orch, "emit") as m_emit:
                cfg = {"runtime": {"escalate_repair_after_attempt": 2}}
                out_cfg, logged = orch._maybe_escalate(
                    cfg, "demo", d, "build_verification", "codex", 1, False)
            self.assertIs(out_cfg, cfg)
            self.assertFalse(logged)
            m_emit.assert_not_called()
            self.assertEqual(mk.load_mistakes(d), [])

    def test_at_threshold_escalates_and_logs_once(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"runtime": {"escalate_repair_after_attempt": 2}}
            with unittest.mock.patch.object(orch, "emit"):
                out_cfg, logged = orch._maybe_escalate(
                    cfg, "demo", d, "build_verification", "codex", 2, False)
            self.assertTrue(logged)
            self.assertIsNot(out_cfg, cfg)
            recs = [r for r in mk.load_mistakes(d) if r["cls"] == "escalation_triggered"]
            self.assertEqual(len(recs), 1)

    def test_already_logged_flag_suppresses_second_ledger_entry(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"runtime": {"escalate_repair_after_attempt": 2}}
            with unittest.mock.patch.object(orch, "emit"):
                _, logged = orch._maybe_escalate(
                    cfg, "demo", d, "k", "codex", 2, False)
                orch._maybe_escalate(cfg, "demo", d, "k", "codex", 3, logged)
            recs = [r for r in mk.load_mistakes(d) if r["cls"] == "escalation_triggered"]
            self.assertEqual(len(recs), 1)

    def test_off_switch_disables_escalation_entirely(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"runtime": {"adaptive_escalation_enabled": False,
                               "escalate_repair_after_attempt": 1}}
            with unittest.mock.patch.object(orch, "emit") as m_emit:
                out_cfg, logged = orch._maybe_escalate(
                    cfg, "demo", d, "k", "codex", 5, False)
            self.assertIs(out_cfg, cfg)
            self.assertFalse(logged)
            m_emit.assert_not_called()
            self.assertEqual(mk.load_mistakes(d), [])


class TestVerifyRepairLoopEscalation(unittest.TestCase):
    """_verify_and_repair's repair loop: escalate on the configured attempt,
    not before; revert once the loop is done (cfg is never mutated)."""

    def _run(self, repair_iterations=3, escalate_after=2, enabled=True, coord="codex"):
        app_dir = tempfile.mkdtemp()
        build_dir = os.path.join(app_dir, "app_build")
        os.makedirs(build_dir)
        md_path = os.path.join(app_dir, "build.md")
        cfg = {
            "runtime": {"verify_build_enabled": True,
                       "adaptive_escalation_enabled": enabled,
                       "escalate_repair_after_attempt": escalate_after},
            "models": {"codex_reasoning": "low", "claude_reasoning": ""},
            "_build_dir": build_dir, "_workflow_name": "app_build",
        }
        phasedef = wf.Phase("build_verification", ".", "build.md", "build",
                            writes=True,
                            verify={"type": "shell", "command": "false",
                                   "repair_iterations": repair_iterations})
        seen = []

        def fake_call_agent(acfg, _app, _phase, _rnd, _agent, _prompt):
            seen.append(dict(acfg.get("models") or {}))
            return "did some repair work"

        failed = {"ran": True, "ok": False, "tool": "shell",
                  "summary": "compile FAILED", "errors": "boom"}
        with unittest.mock.patch.object(orch.verifylib, "run_verification",
                                        lambda *a, **k: dict(failed)), \
             unittest.mock.patch.object(orch, "call_agent", fake_call_agent):
            orch._verify_and_repair(cfg, "demo", app_dir, phasedef,
                                    {"prompt_hash": "h"}, md_path, "", coord)
        return cfg, seen, app_dir

    def test_escalates_from_configured_attempt_onward(self):
        cfg, seen, app_dir = self._run(repair_iterations=3, escalate_after=2)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0]["codex_reasoning"], "low")   # attempt 1: unescalated
        self.assertEqual(seen[1]["codex_reasoning"], "high")  # attempt 2: escalated
        self.assertEqual(seen[2]["codex_reasoning"], "high")  # attempt 3: still escalated

    def test_not_escalated_before_threshold(self):
        cfg, seen, app_dir = self._run(repair_iterations=2, escalate_after=5)
        self.assertTrue(all(m["codex_reasoning"] == "low" for m in seen))

    def test_original_cfg_never_mutated(self):
        cfg, seen, app_dir = self._run(repair_iterations=3, escalate_after=2)
        self.assertEqual(cfg["models"]["codex_reasoning"], "low")

    def test_off_switch_restores_old_behavior(self):
        cfg, seen, app_dir = self._run(repair_iterations=3, escalate_after=2,
                                       enabled=False)
        self.assertTrue(all(m["codex_reasoning"] == "low" for m in seen))
        recs = [r for r in mk.load_mistakes(app_dir) if r["cls"] == "escalation_triggered"]
        self.assertEqual(recs, [])

    def test_log_and_ledger_fire_exactly_once(self):
        cfg, seen, app_dir = self._run(repair_iterations=3, escalate_after=2)
        recs = [r for r in mk.load_mistakes(app_dir) if r["cls"] == "escalation_triggered"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["phase"], "build_verification")
        self.assertEqual(recs[0]["agent"], "codex")

    def test_escalation_does_not_leak_into_a_fresh_verify_cycle(self):
        cfg, seen, app_dir = self._run(repair_iterations=3, escalate_after=2)
        # A brand-new call with the SAME (untouched) cfg starts unescalated again.
        md_path2 = os.path.join(app_dir, "build2.md")
        phasedef = wf.Phase("build_verification", ".", "build.md", "build",
                            writes=True,
                            verify={"type": "shell", "command": "false",
                                   "repair_iterations": 1})
        seen2 = []

        def fake_call_agent(acfg, _app, _phase, _rnd, _agent, _prompt):
            seen2.append(dict(acfg.get("models") or {}))
            return "repair"

        failed = {"ran": True, "ok": False, "tool": "shell",
                  "summary": "compile FAILED", "errors": "boom"}
        with unittest.mock.patch.object(orch.verifylib, "run_verification",
                                        lambda *a, **k: dict(failed)), \
             unittest.mock.patch.object(orch, "call_agent", fake_call_agent):
            orch._verify_and_repair(cfg, "demo", app_dir, phasedef,
                                    {"prompt_hash": "h2"}, md_path2, "", "codex")
        self.assertEqual(seen2[0]["codex_reasoning"], "low")


class TestQualityGateRetryEscalation(unittest.TestCase):
    """process_phase's quality-gate retry path escalates analogously."""

    def test_quality_gate_retry_escalates_on_second_call(self):
        app_dir = tempfile.mkdtemp()
        phase = wf.Phase("app_features", "app_features", "app_features.md",
                         "Prioritize features.", rounds=3)
        cfg = {
            "agents": {"codex_enabled": True, "claude_enabled": False,
                      "gemini_enabled": False, "ollama_enabled": False},
            "runtime": {
                "phase_independent_first_round_enabled": True,
                "phase_quality_gates_enabled": True,
                "phase_quality_repair_rounds": 2,
                "adaptive_escalation_enabled": True,
                "escalate_repair_after_attempt": 2,
            },
            "models": {"codex_reasoning": "low"},
            "_workflow_target": "app",
        }
        state = orch.load_state(app_dir)
        quality_cfgs = []

        def fake_call_agent(_cfg, _app, _phase, rnd, agent, prompt):
            if "PHASE QUALITY EVALUATOR" in prompt:
                quality_cfgs.append((_cfg.get("models") or {}).get("codex_reasoning"))
                if len(quality_cfgs) < 3:
                    return "QUALITY: FAIL\n## Feedback\nMissing acceptance criteria."
                return "QUALITY: PASS\n## Feedback\nGood."
            if "wrapping up round" in prompt:
                return "CONSENSUS: YES\n## Final Output\nSome feature list."
            return "agent discussion"

        orig = orch.call_agent
        orch.call_agent = fake_call_agent
        try:
            orch.process_phase(cfg, "demo", app_dir, phase,
                               "Build a useful iOS app.", [], state, phase_index=0)
        finally:
            orch.call_agent = orig

        self.assertEqual(len(quality_cfgs), 3)
        self.assertEqual(quality_cfgs[0], "low")    # 1st call: unescalated
        self.assertEqual(quality_cfgs[1], "high")   # 2nd call: escalated
        self.assertEqual(quality_cfgs[2], "high")   # 3rd call: still escalated
        recs = [r for r in mk.load_mistakes(app_dir) if r["cls"] == "escalation_triggered"]
        self.assertEqual(len(recs), 1)
        # cfg passed into process_phase is untouched afterwards.
        self.assertEqual(cfg["models"]["codex_reasoning"], "low")


if __name__ == "__main__":
    unittest.main()
