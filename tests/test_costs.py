"""V3 6.4: token/cost accounting — provider parsing, integer micro-USD price
math, unknown-model honesty (never guess a price), rollup aggregation with
metered/unmetered/unpriced kept distinct, and malformed-line tolerance.
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import costs
import events as evlib
import orchestrator as orch
import traces as traceslib


class TestParseUsage(unittest.TestCase):
    def test_provider_stash_shapes(self):
        # The exact dicts the three api: runners + run_local stash via traces.
        self.assertEqual(
            costs.parse_usage({"provider": "anthropic", "model": "m",
                               "prompt_tokens": 11, "completion_tokens": 7}),
            ("anthropic", "m", 11, 7))
        # run_local: counts only, no provider/model -> metered but unpriced.
        self.assertEqual(
            costs.parse_usage({"prompt_tokens": 3, "completion_tokens": 4}),
            (None, None, 3, 4))

    def test_absent_or_malformed_is_none_never_raises(self):
        for bad in (None, {}, [], "x", 7,
                    {"prompt_tokens": "9", "completion_tokens": 1},
                    {"prompt_tokens": 9},
                    {"prompt_tokens": -1, "completion_tokens": 1},
                    {"prompt_tokens": True, "completion_tokens": 1}):
            self.assertIsNone(costs.parse_usage(bad), bad)


class TestPriceMath(unittest.TestCase):
    def test_known_models_price_in_integer_micro_usd(self):
        # 1M in + 1M out on sonnet-5 = $3 + $15 = 18_000_000 micro-USD.
        self.assertEqual(
            costs.cost_micro_usd("anthropic", "claude-sonnet-5", 1_000_000,
                                 1_000_000), 18_000_000)

    def test_full_precision_fixture_matches_hand_computation(self):
        # opus-4-8 @ 5/25 $/MTok: 123_457 in, 987_654 out.
        # in: 123457*5_000_000 = 617_285_000_000; out: 987654*25_000_000
        # = 24_691_350_000_000; sum 25_308_635_000_000; +500_000 round;
        # // 1M = 25_308_635  (== $25.308635)
        self.assertEqual(
            costs.cost_micro_usd("anthropic", "claude-opus-4-8", 123_457,
                                 987_654), 25_308_635)

    def test_longest_prefix_wins(self):
        # claude-sonnet-5* must match the sonnet-5 rate even though
        # "claude-sonnet-4" shares a shorter common prefix family.
        self.assertEqual(
            costs.price_for("anthropic", "claude-sonnet-5-20990101"),
            costs.PRICES[("anthropic", "claude-sonnet-5")])

    def test_unknown_model_or_provider_is_none_never_a_guess(self):
        self.assertIsNone(costs.cost_micro_usd("anthropic", "mystery-model",
                                               10, 10))
        self.assertIsNone(costs.cost_micro_usd("openai", "gpt-5", 10, 10))
        self.assertIsNone(costs.cost_micro_usd("google", "gemini-x", 10, 10))
        self.assertIsNone(costs.cost_micro_usd(None, None, 10, 10))

    def test_zero_tokens_zero_cost(self):
        self.assertEqual(
            costs.cost_micro_usd("anthropic", "claude-haiku-4-5", 0, 0), 0)


class TestRecords(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_metered_and_unmetered_records(self):
        metered = costs.turn_record(
            "api:anthropic:claude-haiku-4-5",
            {"provider": "anthropic", "model": "claude-haiku-4-5",
             "prompt_tokens": 100, "completion_tokens": 50}, "t1")
        self.assertTrue(metered["metered"])
        self.assertEqual(metered["cost_micro_usd"], 350)  # 100*1+50*5 /1k... integer path
        unmetered = costs.turn_record("claude", None, "t2")
        self.assertFalse(unmetered["metered"])
        self.assertIsNone(unmetered["cost_micro_usd"])
        self.assertIsNone(unmetered["input_tokens"])

    def test_record_turn_appends_and_reads_back(self):
        rec = costs.turn_record("agent", {"prompt_tokens": 1,
                                          "completion_tokens": 2}, "t")
        self.assertTrue(costs.record_turn(self.dir, rec))
        self.assertTrue(costs.record_turn(self.dir, rec))
        back = costs.read_records(self.dir)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0]["v"], costs.SCHEMA_VERSION)

    def test_record_turn_is_best_effort_never_raises(self):
        self.assertFalse(costs.record_turn(
            os.path.join(self.dir, "no", "such", "dir"), {"v": 1}))

    def test_malformed_lines_skipped_on_read(self):
        with open(costs.costs_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write('{"v": 1, "metered": false}\n')
            fh.write("not json at all\n")
            fh.write("[1, 2, 3]\n")
            fh.write('{"v": 1, "metered": true, "input_tokens": 5, '
                     '"output_tokens": 6, "cost_micro_usd": 7}\n')
        recs = costs.read_records(self.dir)
        self.assertEqual(len(recs), 2)

    def test_missing_file_reads_empty(self):
        self.assertEqual(costs.read_records(self.dir), [])
        self.assertEqual(costs.rollup(self.dir)["metered_turns"], 0)


class TestRollup(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, app_id, recs):
        d = os.path.join(self.root, app_id)
        os.makedirs(d, exist_ok=True)
        with open(costs.costs_path(d), "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_mixed_metered_unmetered_unpriced_stay_distinct(self):
        recs = [
            costs.turn_record("api:anthropic:claude-haiku-4-5",
                              {"provider": "anthropic",
                               "model": "claude-haiku-4-5",
                               "prompt_tokens": 1000,
                               "completion_tokens": 1000}, "t"),
            costs.turn_record("api:openai:gpt-x",
                              {"provider": "openai", "model": "gpt-x",
                               "prompt_tokens": 10,
                               "completion_tokens": 10}, "t"),
            costs.turn_record("codex", None, "t"),
        ]
        self._write("proj", recs)
        t = costs.rollup(os.path.join(self.root, "proj"))
        self.assertEqual(t["metered_turns"], 2)
        self.assertEqual(t["unmetered_turns"], 1)
        self.assertEqual(t["unpriced_turns"], 1)   # openai turn: tokens, no price
        self.assertEqual(t["tokens_in"], 1010)
        self.assertEqual(t["tokens_out"], 1010)
        self.assertEqual(t["cost_micro_usd"], 6000)  # haiku only: 1k*1+1k*5 per MTok
        # Denominator completeness: every recorded turn is counted somewhere.
        self.assertEqual(t["metered_turns"] + t["unmetered_turns"], 3)

    def test_workspace_aggregates_project_and_section(self):
        metered = costs.turn_record(
            "api:anthropic:claude-haiku-4-5",
            {"provider": "anthropic", "model": "claude-haiku-4-5",
             "prompt_tokens": 1_000_000, "completion_tokens": 0}, "t")
        self._write("flatproj", [metered])
        self._write("nested/ideas/chat-1", [metered, metered])
        self._write("nested/research/chat-2", [metered])
        ids = ["flatproj", "nested/ideas/chat-1", "nested/research/chat-2"]
        w = costs.rollup_workspace(self.root, ids)
        self.assertEqual(w["total"]["metered_turns"], 4)
        self.assertEqual(w["per_project"]["nested"]["metered_turns"], 3)
        self.assertEqual(w["per_project"]["flatproj"]["metered_turns"], 1)
        self.assertEqual(w["per_section"]["nested/ideas"]["metered_turns"], 2)
        self.assertNotIn("flatproj", w["per_section"])   # flat: no section
        self.assertEqual(w["total"]["cost_micro_usd"], 4_000_000)  # 4 * $1
        self.assertEqual(w["schema_version"], costs.SCHEMA_VERSION)

    def test_empty_workspace(self):
        w = costs.rollup_workspace(self.root, [])
        self.assertEqual(w["total"], costs._empty_totals())
        w2 = costs.rollup_workspace(self.root, None)
        self.assertEqual(w2["total"]["metered_turns"], 0)


class TestTurnWiring(unittest.TestCase):
    """_call_agent_once -> costs.jsonl + turn_completed token fields
    (pattern: tests/test_events.py TestTurnEvents)."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.cfg = {"root": os.path.dirname(self.app_dir),
                    "_app_dir": self.app_dir,
                    "runtime": {"provider_min_gap_seconds": 0,
                                "global_worker_cap_enabled": False}}
        self.quiet = orch._QUIET
        orch._QUIET = True
        traceslib.pop_last_usage()

    def tearDown(self):
        orch._QUIET = self.quiet
        traceslib.pop_last_usage()
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _run(self, runner, agent="claude"):
        with unittest.mock.patch.object(orch, "resolve_runner",
                                        lambda _a: runner), \
                unittest.mock.patch.object(orch, "write_call_log",
                                           lambda *a, **kw: None):
            return orch._call_agent_once(self.cfg, "appx", "tech_specs", 1,
                                         agent, "hi")

    def test_metered_api_turn_records_cost_and_event_fields(self):
        def runner(cfg, prompt, timeout):
            traceslib.set_last_usage({
                "provider": "anthropic", "model": "claude-haiku-4-5",
                "prompt_tokens": 1000, "completion_tokens": 1000})
            return ("useful output", "", 0, "api:anthropic model=h")

        self._run(runner, agent="claude")   # agent id irrelevant to metering
        recs = costs.read_records(self.app_dir)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["metered"])
        self.assertEqual(recs[0]["cost_micro_usd"], 6000)
        done = [e for e in evlib.read_events(self.app_dir,
                                             kinds=("turn_completed",))][-1]
        self.assertEqual(done["tokens_in"], 1000)
        self.assertEqual(done["cost_micro_usd"], 6000)

    def test_cli_turn_records_unmetered_and_no_event_token_fields(self):
        def runner(cfg, prompt, timeout):
            return ("useful output", "", 0, "claude -p")

        self._run(runner)
        recs = costs.read_records(self.app_dir)
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0]["metered"])
        self.assertIsNone(recs[0]["cost_micro_usd"])
        done = [e for e in evlib.read_events(self.app_dir,
                                             kinds=("turn_completed",))][-1]
        # emit_event drops None fields: no fake $0, no token keys at all.
        self.assertNotIn("tokens_in", done)
        self.assertNotIn("cost_micro_usd", done)

    def test_billed_failure_still_records_spend(self):
        # Empty output fails the turn, but the provider already billed it.
        def runner(cfg, prompt, timeout):
            traceslib.set_last_usage({
                "provider": "anthropic", "model": "claude-haiku-4-5",
                "prompt_tokens": 500, "completion_tokens": 0})
            return ("", "", 0, "api:anthropic model=h")

        with self.assertRaises(orch.AgentError):
            self._run(runner)
        recs = costs.read_records(self.app_dir)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["metered"])
        self.assertEqual(recs[0]["input_tokens"], 500)

    def test_unbilled_failure_records_nothing(self):
        # A refused/failed turn with no usage must not pollute the meter's
        # denominator (it isn't a completed turn).
        def runner(cfg, prompt, timeout):
            return ("", "refused", 1, "api:openai model=g")

        with self.assertRaises(orch.AgentError):
            self._run(runner)
        self.assertEqual(costs.read_records(self.app_dir), [])

    def test_all_none_usage_stash_on_failure_records_nothing(self):
        # Ollama omits eval counts on degenerate output: the stash exists but
        # carries no real numbers. A truthy-dict gate would write a spurious
        # unmetered record for this UNBILLED failure — the gate must check
        # parseable counts, not dict truthiness.
        def runner(cfg, prompt, timeout):
            traceslib.set_last_usage({"prompt_tokens": None,
                                      "completion_tokens": None})
            return ("", "", 0, "ollama:generate model=m")

        with self.assertRaises(orch.AgentError):
            self._run(runner)
        self.assertEqual(costs.read_records(self.app_dir), [])


if __name__ == "__main__":
    unittest.main()
