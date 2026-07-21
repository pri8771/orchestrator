import json
import os
import signal
import unittest
import unittest.mock

import conductor

from .harness import RecordedEval, canonical_result


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CORE = ("empty_stream", "happy_pipeline", "oscillation_stall", "hop_budget",
        "goal_met", "quiescence", "budget_halt", "capability_pending",
        "failure_route")


class EvalCase(unittest.TestCase):
    def eval(self, name):
        ev = RecordedEval(os.path.join(FIXTURES, name))
        self.addCleanup(ev.close)
        return ev


class TestRecordedStreams(EvalCase):
    def test_all_core_fixtures_drive_real_stack(self):
        for name in CORE:
            with self.subTest(fixture=name):
                ev = self.eval(name)
                result = ev.replay(dial="full_auto")
                ev.assert_expected(self, result)

    def test_termination_layers_are_precise_and_exclusive(self):
        wanted = {
            "goal_met": {"eval/documentation/goal": "goal_met"},
            "oscillation_stall": {
                "eval/ideas/oscillation": "stalled"},
            "quiescence": {
                "eval/documentation/quiescent": "converged_open_items"},
            "budget_halt": {"__workspace__": "turns_exhausted"},
        }
        for name, terminal in wanted.items():
            with self.subTest(fixture=name):
                ev = self.eval(name)
                result = ev.replay(dial="full_auto")
                self.assertEqual(terminal, result["terminal"])
                terminal_lines = [d for d in result["decisions"]
                                  if d in {"goal_met", "stalled",
                                           "converged_open_items",
                                           "budget_exhausted"}]
                self.assertEqual(1, len(terminal_lines))

    def test_oscillation_routes_only_failure_notice_and_hop_budget_is_visible(self):
        oscillation = self.eval("oscillation_stall").replay("full_auto")
        self.assertEqual(oscillation["decisions"],
                         ["stalled", "route_proposed", "route_approved"])
        self.assertTrue(all(route["target"] == "notification"
                            for route in oscillation["routes"]))
        self.assertEqual([], oscillation["delegations"])
        hop = self.eval("hop_budget").replay("full_auto")
        self.assertEqual(["route_proposed", "budget_exhausted"],
                         hop["decisions"])
        self.assertEqual([], hop["delegations"])

    def test_results_are_byte_deterministic(self):
        outputs = []
        for _ in range(2):
            ev = self.eval("happy_pipeline")
            outputs.append(canonical_result(ev.replay("full_auto")))
        self.assertEqual(outputs[0], outputs[1])

    def test_mutated_golden_is_detected_with_diagnostic_context(self):
        ev = self.eval("happy_pipeline")
        result = ev.replay("full_auto")
        wrong = dict(ev.expected)
        wrong["decisions"] = ["wrong"]
        with self.assertRaises(AssertionError) as caught:
            ev.assert_expected(self, result, wrong)
        message = str(caught.exception)
        self.assertIn("fixture happy_pipeline step 0", message)
        self.assertIn("expected", message)
        self.assertIn("actual", message)
        self.assertIn("ledger tail", message)


class TestDialMatrix(EvalCase):
    def test_forward_route_under_all_four_dials(self):
        expected = {
            "full_auto": ["route_proposed", "route_approved"],
            "loops_gated": ["route_proposed", "route_approved"],
            "suggest_only": ["approval_requested"],
            "gated": ["approval_requested"],
        }
        for dial, decisions in expected.items():
            with self.subTest(dial=dial):
                ev = self.eval("happy_pipeline")
                ev.records = ev.records[:1]
                ev.expected["extra_polls"] = 0
                result = ev.replay(dial)
                self.assertEqual(decisions, result["decisions"])

    def test_capability_escalation_pends_under_every_dial(self):
        for dial in conductor.OVERSIGHT_DIALS:
            with self.subTest(dial=dial):
                result = self.eval("capability_pending").replay(dial)
                self.assertEqual(["approval_requested"], result["decisions"])
                self.assertEqual(1, len(result["pending"]))
                self.assertEqual([], result["delegations"])


class TestCrashKillMatrix(EvalCase):
    STAGES = ("pre_guard", "post_route_id", "post_ledger_append",
              "mid_inbox_injection", "post_act_pre_record")

    def test_sigkill_restart_is_exactly_once_at_every_boundary(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                ev = self.eval("happy_pipeline")
                ev.records = ev.records[:1]
                ev.prepare(ev.records)
                ev.state["oversight"] = {"dial": "full_auto"}
                pid = os.fork()
                if pid == 0:
                    os.environ["ORCH_CONDUCTOR_EVAL_CRASH"] = stage
                    with ev.recorded_classifier():
                        conductor.full_poll(
                            ev.root, ev.state, emit=lambda *_a: None,
                            route_engine=conductor.route_engine)
                    os._exit(91)
                _pid, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFSIGNALED(status), stage)
                self.assertEqual(signal.SIGKILL, os.WTERMSIG(status), stage)
                state = conductor.load_conductor_state(ev.root)
                state = conductor.reconcile_on_start(
                    ev.root, state, emit=lambda *_a: None)
                ev.state = state
                ev.poll(1)
                result = ev.result()
                self.assertEqual(1, result["decisions"].count(
                    "route_proposed"), stage)
                terminal = [d for d in result["decisions"]
                            if d in ("route_approved", "route_recovered")]
                self.assertEqual(1, len(terminal), stage)
                self.assertEqual(1, len(result["delegations"]), stage)
                self.assertEqual(1, len(set(
                    d["route_id"] for d in result["delegations"])), stage)
                terminal_record = next(
                    r for r in result["ledger"]
                    if r.get("decision") in
                    ("route_approved", "route_recovered"))
                self.assertEqual(result["delegations"][0]["route_id"],
                                 terminal_record["route_id"], stage)


class TestShippingGuard(unittest.TestCase):
    def test_default_dial_is_not_full_auto(self):
        self.assertNotEqual("full_auto", conductor.DEFAULT_OVERSIGHT_DIAL)
        self.assertEqual(conductor.DEFAULT_OVERSIGHT_DIAL,
                         conductor.default_state()["oversight"]["dial"])

    def test_crash_hook_is_inert_without_env(self):
        with unittest.mock.patch.dict(
                os.environ, {"ORCH_CONDUCTOR_EVAL_CRASH": ""}):
            conductor._eval_crash("pre_guard")


if __name__ == "__main__":
    unittest.main()
