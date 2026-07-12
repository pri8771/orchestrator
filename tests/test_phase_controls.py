"""Per-phase controls added for per-run customization: routed rounds
(0 = unlimited) and free-text instructions in model_routing.json, per-project
fallback-chain overrides, the local-model RAM gate, provider pacing config,
and the adherence-gate JSON contract."""
import json
import os
import tempfile
import unittest

import modelrouting as mrlib
import localmodels as lmlib
import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_routing(dirpath, payload):
    with open(os.path.join(dirpath, "model_routing.json"), "w",
              encoding="utf-8") as fh:
        json.dump(payload, fh)


class TestRoutedRoundsAndInstructions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_rounds_parsed_including_zero(self):
        _write_routing(self.dir, {"phases": {
            "design": {"rounds": 0},
            "build_coordination": {"rounds": 7},
            "bad": {"rounds": "lots"},
            "toobig": {"rounds": 500},
        }})
        r = mrlib.load_routing(self.dir)
        self.assertEqual(r["phases"]["design"]["rounds"], 0)
        self.assertEqual(r["phases"]["build_coordination"]["rounds"], 7)
        self.assertNotIn("bad", r["phases"])
        self.assertNotIn("toobig", r["phases"])

    def test_instructions_sanitized_and_capped(self):
        _write_routing(self.dir, {"phases": {
            "design": {"instructions": "Use SF Symbols.\nDark mode\x00 required.\t OK"},
            "spam": {"instructions": "x" * 9000},
        }})
        r = mrlib.load_routing(self.dir)
        instr = r["phases"]["design"]["instructions"]
        self.assertIn("Use SF Symbols.", instr)
        self.assertIn("\n", instr)          # newlines survive
        self.assertNotIn("\x00", instr)     # control chars stripped
        self.assertLessEqual(len(r["phases"]["spam"]["instructions"]), 4000)

    def test_apply_phase_routing_carries_rounds_and_instructions(self):
        _write_routing(self.dir, {"phases": {
            "design": {"rounds": 0, "instructions": "Ship dark mode."}}})
        cfg = {"_routing": mrlib.load_routing(self.dir), "models": {},
               "_resolved": {}}
        c = orch._apply_phase_routing(cfg, "design")
        self.assertEqual(c["_routed_rounds"], 0)
        self.assertEqual(c["_phase_instructions"], "Ship dark mode.")
        # An unrouted phase carries neither.
        c2 = orch._apply_phase_routing(cfg, "other_phase")
        self.assertNotIn("_routed_rounds", c2)
        self.assertNotIn("_phase_instructions", c2)

    def test_build_context_injects_instructions(self):
        cfg = {"_phase_instructions": "Ship dark mode."}
        ctx = orch.build_context(cfg, "app", ("design", "f", "f.md", "purpose"),
                                 "prompt", [], "")
        self.assertIn("OPERATOR INSTRUCTIONS FOR THIS PHASE", ctx)
        self.assertIn("Ship dark mode.", ctx)


class TestProjectFallbackOverrides(unittest.TestCase):
    def setUp(self):
        self.fleet = tempfile.mkdtemp()
        self.app = tempfile.mkdtemp()

    def test_project_chain_wins_fleet_chain_inherits(self):
        _write_routing(self.fleet, {"fallback": {
            "chains": {"claude": ["claude-haiku-4-5"],
                       "codex": ["gpt-5.3-codex-spark"]}}})
        _write_routing(self.app, {"fallback": {
            "chains": {"claude": ["local:qwen2.5-coder:14b"]}}})
        merged = mrlib.load_routing_for_app(self.fleet, self.app)
        self.assertEqual(merged["fallback"]["chains"]["claude"],
                         ["local:qwen2.5-coder:14b"])   # project wins
        self.assertEqual(merged["fallback"]["chains"]["codex"],
                         ["gpt-5.3-codex-spark"])       # fleet inherited

    def test_roundtripped_defaults_do_not_shadow_fleet(self):
        _write_routing(self.fleet, {"enabled": True, "fallback": {
            "cloud_to_local": False,
            "chains": {"gemini": ["gemini-3.5-flash"]}}})
        # A GUI save with no project edits: default fallback block, no phases.
        _write_routing(self.app, {"enabled": True, "fallback": {
            "cloud_to_local": True, "local_model": "", "chains": {}},
            "phases": {}})
        merged = mrlib.load_routing_for_app(self.fleet, self.app)
        self.assertFalse(merged["fallback"]["cloud_to_local"])
        self.assertEqual(merged["fallback"]["chains"]["gemini"],
                         ["gemini-3.5-flash"])

    def test_project_local_model_wins(self):
        _write_routing(self.fleet, {"fallback": {"local_model": "gemma3:4b"}})
        _write_routing(self.app, {"fallback": {"local_model": "qwen2.5vl:3b"},
                                  "phases": {}})
        merged = mrlib.load_routing_for_app(self.fleet, self.app)
        self.assertEqual(merged["fallback"]["local_model"], "qwen2.5vl:3b")


class TestRamGate(unittest.TestCase):
    def _cfg(self, roster, gate=True):
        return {
            "agents": {"ollama_enabled": True, "codex_enabled": False,
                       "claude_enabled": False, "gemini_enabled": False},
            "models": {"ollama": "", "ollama_roster": roster},
            "runtime": {"enforce_local_ram_gate": gate,
                        "skip_uninstalled_local_models": False},
        }

    def test_too_big_models_benched(self):
        real = lmlib.total_ram_gb
        lmlib.total_ram_gb = lambda: 16
        try:
            # qwen3-coder:30b needs 32GB per the registry; 16GB machine benches it.
            active = orch.enabled_agents(
                self._cfg("qwen3-coder:30b, gemma3:4b"))
            self.assertEqual(active, ["local:gemma3:4b"])
        finally:
            lmlib.total_ram_gb = real

    def test_gate_disabled_keeps_roster(self):
        real = lmlib.total_ram_gb
        lmlib.total_ram_gb = lambda: 16
        try:
            active = orch.enabled_agents(
                self._cfg("qwen3-coder:30b, gemma3:4b", gate=False))
            self.assertIn("local:qwen3-coder:30b", active)
        finally:
            lmlib.total_ram_gb = real

    def test_total_ram_detects_something_here(self):
        self.assertGreater(lmlib.total_ram_gb(), 0)


class TestProviderPacing(unittest.TestCase):
    def test_zero_gap_disables(self):
        cfg = {"runtime": {"provider_min_gap_seconds": 0}}
        t0 = __import__("time").time()
        orch._pace_provider(cfg, "claude")
        orch._pace_provider(cfg, "claude")
        self.assertLess(__import__("time").time() - t0, 0.5)

    def test_local_agents_never_paced(self):
        cfg = {"runtime": {"provider_min_gap_seconds": 60}}
        t0 = __import__("time").time()
        orch._pace_provider(cfg, "local:gemma3:4b")
        orch._pace_provider(cfg, "ollama")
        self.assertLess(__import__("time").time() - t0, 0.5)

    def test_second_same_provider_call_waits(self):
        import time as _t
        cfg = {"runtime": {"provider_min_gap_seconds": 1.2}}
        orch._PACE_LAST.pop("codex", None)
        orch._pace_provider(cfg, "codex")
        t0 = _t.time()
        orch._pace_provider(cfg, "codex")
        self.assertGreaterEqual(_t.time() - t0, 1.0)
        orch._PACE_LAST.pop("codex", None)


class TestAdherenceGate(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "app_build"), exist_ok=True)
        with open(os.path.join(self.app_dir, "app_build", "App.swift"), "w") as fh:
            fh.write("// entry\n")

    class _WF:
        target = "app"
        build_phase = "build_coordination"

    def _phases(self):
        return [{"verify": {"type": "xcodebuild"}}]

    def test_disabled_gate_returns_none(self):
        cfg = {"runtime": {"adherence_gate": False}}
        self.assertIsNone(orch._adherence_gate(
            cfg, "x", self.app_dir, self._phases(), {}, "prompt", self._WF()))

    def test_fail_verdict_returns_reason_and_persists(self):
        cfg = {"runtime": {"adherence_gate": True}}
        reply = ("Judgment.\n```adherence-json\n"
                 + json.dumps({"score": 40,
                               "requirements": [{"text": "offline sync",
                                                 "grade": "UNMET"}],
                               "verdict": "FAIL",
                               "reason": "core feature missing"})
                 + "\n```")
        real = (orch.enabled_agents, orch._pick_coordinator, orch.call_agent)
        orch.enabled_agents = lambda c: ["claude"]
        orch._pick_coordinator = lambda c, a: "claude"
        orch.call_agent = lambda *a, **k: reply
        try:
            reason = orch._adherence_gate(
                cfg, "x", self.app_dir, self._phases(), {"phase_outputs": {}},
                "prompt", self._WF())
        finally:
            orch.enabled_agents, orch._pick_coordinator, orch.call_agent = real
        self.assertIn("core feature missing", reason)
        self.assertIn("offline sync", reason)
        with open(os.path.join(self.app_dir, "docs", "adherence.json")) as fh:
            self.assertEqual(json.load(fh)["verdict"], "FAIL")

    def test_pass_verdict_returns_none(self):
        cfg = {"runtime": {"adherence_gate": True}}
        reply = ("```adherence-json\n"
                 + json.dumps({"score": 92, "requirements": [],
                               "verdict": "PASS", "reason": "solid"})
                 + "\n```")
        real = (orch.enabled_agents, orch._pick_coordinator, orch.call_agent)
        orch.enabled_agents = lambda c: ["claude"]
        orch._pick_coordinator = lambda c, a: "claude"
        orch.call_agent = lambda *a, **k: reply
        try:
            self.assertIsNone(orch._adherence_gate(
                cfg, "x", self.app_dir, self._phases(), {"phase_outputs": {}},
                "prompt", self._WF()))
        finally:
            orch.enabled_agents, orch._pick_coordinator, orch.call_agent = real

    def test_spec_workflow_exempt(self):
        cfg = {"runtime": {"adherence_gate": True}}

        class SpecWF:
            target = "app"
            build_phase = None
        self.assertIsNone(orch._adherence_gate(
            cfg, "x", self.app_dir, [{}], {}, "prompt", SpecWF()))


if __name__ == "__main__":
    unittest.main()
