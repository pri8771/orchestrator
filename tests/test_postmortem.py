"""Tests for postmortem.py — the `--postmortem <app>` correlated failure
report (state + events + verify_results + mistakes ledger + turn telemetry).
Fixture app dirs are built with the REAL writers (events.emit_event,
verify.persist_verify_result, mistakes.append_mistake) so the report is tested
against the same on-disk shapes the engine produces."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import events as ev
import mistakes as mk
import postmortem as pm
import verify as verifylib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_state(app_dir, state):
    with open(os.path.join(app_dir, "agent_state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def _aborted_fixture(app_dir):
    """A run that completed two phases, then died in build_coordination:
    one good claude turn, one codex timeout rescued by a fallback, a failed
    verification, and an aborted run_finished."""
    _write_state(app_dir, {
        "workflow": "app_build",
        "current_phase": "build_coordination",
        "completed_phases": ["initial_discussion", "tech_specs"],
        "consensus_status": {"initial_discussion": "consensus",
                             "tech_specs": "consensus",
                             "build_coordination": "no_consensus"},
        "done": False, "error": "Codex timed out after 600s",
        "status": "aborted", "verification": "failed",
        "last_processed": "2026-07-13 10:00:00",
        "fallback_counts": {"codex": 1},
    })
    ev.emit_event(app_dir, "run_started", project="demo", workflow="app_build",
                  phases=5)
    ev.emit_event(app_dir, "turn_started", project="demo", phase="tech_specs",
                  round=1, agent="claude")
    ev.emit_event(app_dir, "turn_completed", project="demo", phase="tech_specs",
                  round=1, agent="claude", ok=True, exit=0,
                  output_len=1000, dur=12.5)
    ev.emit_event(app_dir, "turn_started", project="demo",
                  phase="build_coordination", round=2, agent="codex")
    ev.emit_event(app_dir, "turn_completed", project="demo",
                  phase="build_coordination", round=2, agent="codex",
                  ok=False, exit=124, reason="timeout", dur=600.0)
    ev.emit_event(app_dir, "agent_fallback", project="demo",
                  phase="build_coordination", round=2, agent="codex",
                  from_model="gpt-5-codex", to_model="claude",
                  status="attempt", reason="timed out")
    ev.emit_event(app_dir, "agent_fallback", project="demo",
                  phase="build_coordination", round=2, agent="codex",
                  from_model="gpt-5-codex", to_model="claude",
                  status="rescued", reason="timed out")
    ev.emit_event(app_dir, "agent_disabled", project="demo", agent="gemini",
                  reason="startup probe failed")
    ev.emit_event(app_dir, "verify_result", project="demo",
                  phase="build_coordination", status="failed",
                  detail="attempt 0: 2 compile errors")
    ev.emit_event(app_dir, "run_finished", project="demo", status="aborted",
                  detail="Codex timed out after 600s", verification="failed")
    verifylib.persist_verify_result(
        app_dir, "build_coordination",
        {"ran": True, "ok": False, "summary": "2 compile errors",
         "tool": "xcodebuild"}, attempt=0, workflow="app_build")
    verifylib.persist_verify_result(
        app_dir, "build_coordination",
        {"ran": True, "ok": True, "summary": "build succeeded",
         "tool": "xcodebuild"}, attempt=1, workflow="app_build")
    mk.append_mistake(app_dir, {"app": "demo", "phase": "build_coordination",
                                "cls": "verify_failure",
                                "summary": "attempt 0: 2 compile errors"})
    mk.append_mistake(app_dir, {"app": "demo", "phase": "build_coordination",
                                "agent": "codex", "cls": "agent_fallback",
                                "summary": "gpt-5-codex -> claude (rescued)"})


class TestAbortedRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app_dir = os.path.join(self._tmp.name, "demo")
        os.makedirs(self.app_dir)
        _aborted_fixture(self.app_dir)
        self.rep = pm.build_postmortem(self.app_dir, app="demo")

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_block(self):
        run = self.rep["run"]
        self.assertEqual(run["status"], "aborted")
        self.assertEqual(run["verification"], "failed")
        self.assertEqual(run["workflow"], "app_build")
        self.assertEqual(run["error"], "Codex timed out after 600s")
        self.assertEqual(run["last_position"],
                         {"phase": "build_coordination", "round": 2})

    def test_phase_rows_join_consensus(self):
        rows = {r["phase"]: r for r in self.rep["phases"]}
        self.assertTrue(rows["tech_specs"]["completed"])
        self.assertEqual(rows["tech_specs"]["consensus"], "consensus")
        self.assertFalse(rows["build_coordination"]["completed"])
        self.assertEqual(rows["build_coordination"]["consensus"], "no_consensus")

    def test_failure_chain_contents(self):
        kinds = [e["kind"] for e in self.rep["failure_chain"]]
        # timeout turn, rescued fallback (NOT the "attempt" step), disable,
        # failed verify, aborted run_finished — in event order.
        self.assertEqual(kinds, ["turn_completed", "agent_fallback",
                                 "agent_disabled", "verify_result",
                                 "run_finished"])
        summaries = " | ".join(e["summary"] for e in self.rep["failure_chain"])
        self.assertIn("timed out", summaries)
        self.assertIn("rescued", summaries)
        self.assertNotIn("attempt", summaries)
        self.assertIn("run finished: aborted", summaries)

    def test_verify_attempt_history(self):
        atts = self.rep["verify_attempts"]
        self.assertEqual(len(atts), 2)
        self.assertEqual(atts[0]["status"], "failed")
        self.assertFalse(atts[0]["repair"])
        self.assertEqual(atts[1]["status"], "verified")
        self.assertTrue(atts[1]["repair"])

    def test_mistakes_rollup(self):
        mkr = self.rep["mistakes"]
        self.assertEqual(mkr["total"], 2)
        self.assertEqual(mkr["by_class"],
                         {"verify_failure": 1, "agent_fallback": 1})
        self.assertEqual(mkr["by_phase"], {"build_coordination": 2})
        self.assertEqual(mkr["by_agent"], {"codex": 1})
        self.assertEqual(len(mkr["recent"]), 2)

    def test_telemetry_measured_quantities(self):
        tel = self.rep["telemetry"]
        claude = tel["by_agent"]["claude"]
        self.assertEqual(claude["turns"], 1)
        self.assertEqual(claude["failed_turns"], 0)
        self.assertEqual(claude["total_dur"], 12.5)
        self.assertEqual(claude["mean_dur"], 12.5)
        self.assertEqual(claude["total_output_chars"], 1000)
        codex = tel["by_agent"]["codex"]
        self.assertEqual(codex["turns"], 1)
        self.assertEqual(codex["failed_turns"], 1)
        self.assertEqual(codex["total_dur"], 600.0)
        self.assertEqual(codex["total_output_chars"], 0)
        self.assertEqual(codex["fallbacks"], 1)  # outcome only, not "attempt"
        bc = tel["by_phase"]["build_coordination"]
        self.assertEqual(bc["turns"], 1)
        self.assertEqual(bc["fallbacks"], 1)
        self.assertEqual(tel["by_phase"]["tech_specs"]["total_output_chars"], 1000)

    def test_no_pricing_no_cost_fields(self):
        # Default (pricing=None, same as build_postmortem's default arg): no
        # cost field anywhere, no cost_note — byte-for-byte the pre-cost-
        # estimation shape.
        tel = self.rep["telemetry"]
        for bucket in list(tel["by_phase"].values()) + list(tel["by_agent"].values()):
            self.assertNotIn("est_cost_usd", bucket)
        self.assertNotIn("cost_note", tel)

    def test_json_serializable(self):
        json.dumps(self.rep)

    def test_render_text_sections(self):
        text = pm.render_postmortem(self.rep)
        for needle in ("POSTMORTEM: demo", "aborted", "Failure chain",
                       "Verification attempts", "Mistakes ledger",
                       "Turn telemetry", "build_coordination",
                       "fallbacks=1", "Codex timed out after 600s"):
            self.assertIn(needle, text)


class TestCostEstimation(unittest.TestCase):
    """cost.pricing is opt-in: build_postmortem(pricing=...) only. No config
    file involved here — orchestrator.py's CLI wiring reads cost.pricing and
    passes it through, tested separately (this exercises postmortem.py's own
    contract)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app_dir = os.path.join(self._tmp.name, "demo")
        os.makedirs(self.app_dir)
        _write_state(self.app_dir, {"workflow": "app_build", "done": True,
                                    "status": "done"})
        ev.emit_event(self.app_dir, "turn_completed", project="demo",
                      phase="tech_specs", round=1, agent="claude", ok=True,
                      exit=0, model_used="claude-sonnet-5",
                      output_len=2000, dur=5.0)
        ev.emit_event(self.app_dir, "turn_completed", project="demo",
                      phase="build_coordination", round=1, agent="codex",
                      ok=True, exit=0, model_used="gpt-5.3-codex-spark",
                      output_len=3000, dur=8.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_pricing_arg_no_cost_fields(self):
        rep = pm.build_postmortem(self.app_dir, app="demo")
        tel = rep["telemetry"]
        self.assertNotIn("est_cost_usd", tel["by_agent"]["claude"])
        self.assertNotIn("est_cost_usd", tel["by_agent"]["codex"])
        self.assertNotIn("cost_note", tel)

    def test_pricing_for_one_model_only_that_models_telemetry_gains_cost(self):
        pricing = {"claude-sonnet-5": {"output_per_1k_chars": 0.01}}
        rep = pm.build_postmortem(self.app_dir, app="demo", pricing=pricing)
        tel = rep["telemetry"]
        # claude turn: 2000 chars * 0.01/1000 = 0.02
        self.assertAlmostEqual(tel["by_agent"]["claude"]["est_cost_usd"], 0.02)
        self.assertAlmostEqual(tel["by_phase"]["tech_specs"]["est_cost_usd"], 0.02)
        # codex's model has no configured rate — no cost field at all.
        self.assertNotIn("est_cost_usd", tel["by_agent"]["codex"])
        self.assertNotIn("est_cost_usd", tel["by_phase"]["build_coordination"])
        self.assertIn("cost_note", tel)
        self.assertIn("OUTPUT", tel["cost_note"])

    def test_input_per_1k_chars_accepted_but_unused(self):
        # Configuring an input rate must not raise and must not change the
        # output-only cost estimate (this engine tracks no input chars).
        pricing = {"claude-sonnet-5": {"output_per_1k_chars": 0.01,
                                       "input_per_1k_chars": 999.0}}
        rep = pm.build_postmortem(self.app_dir, app="demo", pricing=pricing)
        self.assertAlmostEqual(
            rep["telemetry"]["by_agent"]["claude"]["est_cost_usd"], 0.02)

    def test_render_text_includes_cost_when_priced(self):
        pricing = {"claude-sonnet-5": {"output_per_1k_chars": 0.01}}
        rep = pm.build_postmortem(self.app_dir, app="demo", pricing=pricing)
        text = pm.render_postmortem(rep)
        self.assertIn("est_cost=$0.0200", text)
        self.assertIn("estimated from operator-supplied", text)

    def test_render_text_omits_cost_when_not_priced(self):
        rep = pm.build_postmortem(self.app_dir, app="demo")
        text = pm.render_postmortem(rep)
        self.assertNotIn("est_cost", text)


class TestCliPricingWiring(unittest.TestCase):
    """orchestrator.py's --postmortem reads cget(cfg, 'cost.pricing') and
    passes it through; the default config.yaml ships an empty pricing dict
    so a real run's --postmortem stays cost-field-free."""

    def test_empty_dict_pricing_yields_no_cost_fields(self):
        with tempfile.TemporaryDirectory() as d:
            app_dir = os.path.join(d, "demo")
            os.makedirs(app_dir)
            _write_state(app_dir, {"workflow": "app_build", "done": True})
            ev.emit_event(app_dir, "turn_completed", project="demo",
                         phase="p", round=1, agent="claude", ok=True, exit=0,
                         model_used="claude-sonnet-5", output_len=100, dur=1.0)
            # cget(cfg, "cost.pricing", None) on the real config.yaml returns
            # {} (empty dict, not None) — build_postmortem must treat that as
            # "no pricing" too (falsy), not attempt lookups against it.
            rep = pm.build_postmortem(app_dir, app="demo", pricing={})
            self.assertNotIn("est_cost_usd", rep["telemetry"]["by_agent"]["claude"])
            self.assertNotIn("cost_note", rep["telemetry"])


class TestCleanRun(unittest.TestCase):
    def test_clean_run_has_empty_failure_chain(self):
        with tempfile.TemporaryDirectory() as d:
            app_dir = os.path.join(d, "demo")
            os.makedirs(app_dir)
            _write_state(app_dir, {
                "workflow": "app_build", "done": True, "error": None,
                "status": "done", "verification": "verified",
                "completed_phases": ["initial_discussion"],
                "consensus_status": {"initial_discussion": "consensus"}})
            ev.emit_event(app_dir, "turn_completed", project="demo",
                          phase="initial_discussion", round=1, agent="claude",
                          ok=True, output_len=500, dur=5.0)
            ev.emit_event(app_dir, "run_finished", project="demo",
                          status="done", verification="verified")
            rep = pm.build_postmortem(app_dir, app="demo")
        self.assertEqual(rep["run"]["status"], "done")
        self.assertEqual(rep["failure_chain"], [])
        self.assertEqual(rep["mistakes"]["total"], 0)
        self.assertEqual(rep["telemetry"]["by_agent"]["claude"]["turns"], 1)
        text = pm.render_postmortem(rep)
        self.assertIn("(no failure events recorded)", text)

    def test_empty_dir_degrades_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            rep = pm.build_postmortem(d)
            json.dumps(rep)
            self.assertEqual(rep["run"]["status"], "running")
            self.assertEqual(rep["phases"], [])
            self.assertEqual(rep["verify_attempts"], [])
            pm.render_postmortem(rep)

    def test_status_derived_when_not_persisted(self):
        # A partial/fixture state without the stamped rollup still gets the
        # derive_run_status rules applied.
        with tempfile.TemporaryDirectory() as d:
            _write_state(d, {"error": "boom"})
            self.assertEqual(pm.build_postmortem(d)["run"]["status"], "aborted")
            _write_state(d, {"done": True})
            self.assertEqual(pm.build_postmortem(d)["run"]["status"], "done")


class TestPostmortemCliRealSubprocess(unittest.TestCase):
    """`python3 orchestrator.py --postmortem --app <name> --json` must print
    pure JSON on stdout (same contract as --doctor/--mistakes --json)."""

    def test_stdout_is_pure_json(self):
        root = tempfile.mkdtemp()
        app_dir = os.path.join(root, "demo")
        os.makedirs(app_dir)
        _aborted_fixture(app_dir)
        env = dict(os.environ)
        env["ORCH_ROOT"] = root
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"),
             "--postmortem", "--app", "demo", "--json"],
            capture_output=True, text=True, timeout=60, env=env, cwd=HERE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        try:
            rep = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail("stdout was not pure JSON (%s): %r" % (exc, proc.stdout[:300]))
        self.assertEqual(rep["schema_version"], 1)
        self.assertEqual(rep["app"], "demo")
        self.assertEqual(rep["run"]["status"], "aborted")
        self.assertTrue(rep["failure_chain"])

    def test_requires_app(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"),
             "--postmortem"],
            capture_output=True, text=True, timeout=60, cwd=HERE)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--postmortem requires --app", proc.stderr)


if __name__ == "__main__":
    unittest.main()
