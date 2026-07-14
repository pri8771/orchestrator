"""Tests for the workflow "overrides" preset (optional top-level JSON key):

- claude_model/codex_model replace the resolved model config for the run
- rounds_scale multiplies each phase's rounds (round to int, min 1)
- effort "fast" caps every phase at 2 rounds (after rounds_scale);
  "max" floors discussion (writes:false) phases at 3; "standard" is a no-op
- workflows WITHOUT "overrides" behave exactly as before
- the three shipped starter workflows (brainstorm/prototype/full_max) load
"""
import os
import tempfile
import unittest

import modelrouting as mr
import orchestrator as orch
import workflows as wf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mk_workflow(overrides=None, rounds=(9, 9), build_rounds=9):
    phases = [
        wf.Phase("initial_discussion", "initial_discussion",
                 "initial_discussion.md", "p", rounds=rounds[0]),
        wf.Phase("tech_specs", "tech_specs", "tech_specs.md", "p",
                 rounds=rounds[1]),
        wf.Phase("build_coordination", "build_coordination",
                 "agent_messages.md", "p", rounds=build_rounds, writes=True),
    ]
    return wf.Workflow("t", "T", "d", phases, overrides=overrides)


def _cfg():
    return {
        "models": {"claude": "sonnet", "codex": "gpt-5"},
        "_resolved": {"claude_model": "sonnet", "codex_model": "gpt-5"},
    }


class TestApplyOverrides(unittest.TestCase):
    def test_model_swap(self):
        cfg = _cfg()
        w = _mk_workflow({"claude_model": "opus", "codex_model": "gpt-5-codex"})
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(orch.cget(cfg, "models.claude"), "opus")
        self.assertEqual(cfg["_resolved"]["claude_model"], "opus")
        self.assertEqual(orch.cget(cfg, "models.codex"), "gpt-5-codex")
        self.assertEqual(cfg["_resolved"]["codex_model"], "gpt-5-codex")
        # rounds untouched when neither rounds_scale nor effort is set
        self.assertEqual([p.rounds for p in w.phases], [9, 9, 9])

    def test_rounds_scale_halves_and_floors_at_one(self):
        cfg = _cfg()
        w = _mk_workflow({"rounds_scale": 0.5}, rounds=(6, 1), build_rounds=6)
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(w.phases[0].rounds, 3)   # 6 * 0.5
        self.assertEqual(w.phases[1].rounds, 1)   # min 1, never 0
        self.assertEqual(w.phases[2].rounds, 3)   # build phases scale too

    def test_fast_caps_at_two_after_scale(self):
        cfg = _cfg()
        w = _mk_workflow({"effort": "fast", "rounds_scale": 2.0},
                         rounds=(9, 1), build_rounds=9)
        orch._apply_workflow_overrides(cfg, w, w.phases)
        # 9 * 2.0 = 18, then the fast cap wins
        self.assertEqual(w.phases[0].rounds, 2)
        self.assertEqual(w.phases[1].rounds, 2)   # 1 * 2.0 = 2
        self.assertEqual(w.phases[2].rounds, 2)   # cap applies to every phase

    def test_max_floors_discussion_phases_only(self):
        cfg = _cfg()
        w = _mk_workflow({"effort": "max"}, rounds=(1, 9), build_rounds=1)
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(w.phases[0].rounds, 3)   # floored (writes:false)
        self.assertEqual(w.phases[1].rounds, 9)   # no cap on max
        self.assertEqual(w.phases[2].rounds, 1)   # writes:true never floored

    def test_standard_effort_is_noop(self):
        cfg = _cfg()
        w = _mk_workflow({"effort": "standard"}, rounds=(9, 1))
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual([p.rounds for p in w.phases], [9, 1, 9])
        self.assertEqual(orch.cget(cfg, "models.claude"), "sonnet")

    def test_no_overrides_unchanged(self):
        cfg = _cfg()
        w = _mk_workflow(None)
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(orch.cget(cfg, "models.claude"), "sonnet")
        self.assertEqual(cfg["_resolved"]["codex_model"], "gpt-5")
        self.assertEqual([p.rounds for p in w.phases], [9, 9, 9])

    def test_preset_never_leaks_into_next_run(self):
        # Same cfg reused (as in --watch): a preset run followed by a
        # no-preset run must land back on the pristine base models.
        cfg = _cfg()
        orch._apply_workflow_overrides(
            cfg, _mk_workflow({"claude_model": "opus"}), [])
        self.assertEqual(orch.cget(cfg, "models.claude"), "opus")
        orch._apply_workflow_overrides(cfg, _mk_workflow(None), [])
        self.assertEqual(orch.cget(cfg, "models.claude"), "sonnet")
        self.assertEqual(cfg["_resolved"]["claude_model"], "sonnet")


class TestClaudeEffortParity(unittest.TestCase):
    """Claude --effort parity (mirrors the codex reasoning pair)."""

    def _run_claude_cmd(self, cfg):
        """Invoke run_claude with a stubbed subprocess; return the built cmd."""
        captured = {}

        def fake_run(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            captured["cmd"] = list(cmd)
            return "ok", "", 0

        orig = orch._run_subprocess
        orch._run_subprocess = fake_run
        try:
            orch.run_claude(cfg, "hi", 10)
        finally:
            orch._run_subprocess = orig
        return captured["cmd"]

    def test_effort_flag_omitted_by_default(self):
        cfg = _cfg()
        cmd = self._run_claude_cmd(cfg)
        self.assertNotIn("--effort", cmd)

    def test_prompt_passed_on_stdin_not_argv(self):
        # The prompt (and any secret in it) must not land on argv where `ps` /
        # /proc/<pid>/cmdline expose it to other local users.
        captured = {}

        def fake_run(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            captured["cmd"] = list(cmd)
            captured["input_text"] = input_text
            return "ok", "", 0

        orig = orch._run_subprocess
        orch._run_subprocess = fake_run
        try:
            orch.run_claude(_cfg(), "SECRET-PROMPT-BODY", 10)
        finally:
            orch._run_subprocess = orig
        self.assertEqual(captured["input_text"], "SECRET-PROMPT-BODY")
        self.assertNotIn("SECRET-PROMPT-BODY", captured["cmd"])
        self.assertEqual(captured["cmd"][:2], ["claude", "-p"])

    def test_chat_turn_uses_claude_reasoning(self):
        cfg = _cfg()
        cfg["models"]["claude_reasoning"] = "low"
        cfg["models"]["claude_build_reasoning"] = "high"
        cmd = self._run_claude_cmd(cfg)
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_build_turn_uses_claude_build_reasoning(self):
        cfg = _cfg()
        cfg["models"]["claude_reasoning"] = "low"
        cfg["models"]["claude_build_reasoning"] = "high"
        cfg["_allow_writes"] = True
        cfg["_build_dir"] = None   # fall through to isolate/root cwd
        cfg["root"] = os.getcwd()
        cmd = self._run_claude_cmd(cfg)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_overrides_effort_fast_maps_to_low(self):
        cfg = _cfg()
        w = _mk_workflow({"effort": "fast"})
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(orch.cget(cfg, "models.claude_reasoning"), "low")
        self.assertEqual(orch.cget(cfg, "models.claude_build_reasoning"), "low")

    def test_overrides_effort_max_maps_to_high(self):
        cfg = _cfg()
        w = _mk_workflow({"effort": "max"})
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(orch.cget(cfg, "models.claude_reasoning"), "high")
        self.assertEqual(orch.cget(cfg, "models.claude_build_reasoning"), "high")

    def test_overrides_effort_standard_leaves_config_value(self):
        cfg = _cfg()
        cfg["models"]["claude_reasoning"] = "medium"
        w = _mk_workflow({"effort": "standard"})
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(orch.cget(cfg, "models.claude_reasoning"), "medium")

    def test_effort_preset_never_leaks_into_next_run(self):
        cfg = _cfg()
        w = _mk_workflow({"effort": "fast"})
        orch._apply_workflow_overrides(cfg, w, w.phases)
        self.assertEqual(orch.cget(cfg, "models.claude_reasoning"), "low")
        w2 = _mk_workflow(None)
        orch._apply_workflow_overrides(cfg, w2, w2.phases)
        self.assertEqual(orch.cget(cfg, "models.claude_reasoning", ""), "")

    def test_phase_routing_claude_reasoning_sets_both_keys(self):
        cfg = _cfg()
        cfg["_routing"] = {"phases": {"tech_specs": {"claude_reasoning": "xhigh"}}}
        scoped = orch._apply_phase_routing(cfg, "tech_specs")
        self.assertEqual(orch.cget(scoped, "models.claude_reasoning"), "xhigh")
        self.assertEqual(orch.cget(scoped, "models.claude_build_reasoning"), "xhigh")
        # ...and only inside the scoped copy.
        self.assertEqual(orch.cget(cfg, "models.claude_reasoning", ""), "")

    def test_phase_routing_claude_reasoning_survives_the_json_file(self):
        # Regression: the test above hand-builds cfg["_routing"], which
        # skips modelrouting.load_routing's field allowlist entirely. Real
        # runs read model_routing.json through that allowlist — it used to
        # drop "claude_reasoning" silently (missing from PHASE_FIELDS), so
        # this knob could never fire outside a test that bypassed the loader.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, mr.ROUTING_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"phases": {"tech_specs": {"claude_reasoning": "xhigh"}}}')
            routing = mr.load_routing(d)
        cfg = _cfg()
        cfg["_routing"] = routing
        scoped = orch._apply_phase_routing(cfg, "tech_specs")
        self.assertEqual(orch.cget(scoped, "models.claude_reasoning"), "xhigh")
        self.assertEqual(orch.cget(scoped, "models.claude_build_reasoning"), "xhigh")


class TestWorkflowJsonRoundtrip(unittest.TestCase):
    def test_overrides_roundtrip(self):
        w = _mk_workflow({"effort": "fast", "rounds_scale": 0.5})
        d = w.to_json()
        self.assertEqual(d["overrides"], {"effort": "fast", "rounds_scale": 0.5})
        w2 = wf.Workflow.from_json(d)
        self.assertEqual(w2.overrides, {"effort": "fast", "rounds_scale": 0.5})

    def test_overrides_serialized_as_null_when_unset(self):
        # to_json now emits every field for a uniform on-disk shape: `overrides`
        # is always present, null when there's no preset.
        d = _mk_workflow(None).to_json()
        self.assertIn("overrides", d)
        self.assertIsNone(d["overrides"])
        self.assertIsNone(wf.Workflow.from_json(
            {"name": "x", "phases": []}).overrides)

    def test_non_dict_overrides_ignored(self):
        self.assertIsNone(wf.Workflow.from_json(
            {"name": "x", "phases": [], "overrides": "fast"}).overrides)


class TestStarterWorkflows(unittest.TestCase):
    def test_brainstorm_loads(self):
        w = wf.load_workflow("brainstorm", HERE)
        self.assertEqual(w.name, "brainstorm")
        self.assertEqual([p.key for p in w.phases],
                         ["prompt_contract", "product_research",
                          "initial_discussion"])
        self.assertEqual(w.overrides, {"effort": "fast"})
        self.assertIsNone(w.build_phase)
        self.assertTrue(all(p.verify is None for p in w.phases))

    def test_prototype_loads(self):
        w = wf.load_workflow("prototype", HERE)
        base = wf.load_workflow("app_build", HERE)
        self.assertEqual(w.name, "prototype")
        self.assertEqual(w.overrides, {"rounds_scale": 0.5})
        keys = [p.key for p in w.phases]
        self.assertNotIn("human_qa_checklist", keys)
        self.assertNotIn("app_store_readiness", keys)
        # prototype is speed-oriented, so unlike full_max it also omits
        # write_tests/real xcodebuild-test execution (item 2: opt-in only for
        # the deep pipelines, app_build/full_max).
        self.assertNotIn("write_tests", keys)
        self.assertEqual(keys, [p.key for p in base.phases
                                if p.key not in ("human_qa_checklist",
                                                 "app_store_readiness",
                                                 "write_tests")])
        self.assertEqual(w.build_phase, "build_coordination")

    def test_full_max_loads(self):
        w = wf.load_workflow("full_max", HERE)
        base = wf.load_workflow("app_build", HERE)
        self.assertEqual(w.name, "full_max")
        self.assertEqual(w.overrides, {"effort": "max"})
        self.assertEqual([p.key for p in w.phases],
                         [p.key for p in base.phases])

    def test_starters_listed(self):
        names = wf.list_workflows(HERE)
        for n in ("brainstorm", "prototype", "full_max"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()
