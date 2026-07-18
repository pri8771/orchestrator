"""V3 board 2.8: LLM call traces — crash-valid from the first byte.

Covers the full schema per call, the started->finalized lifecycle on
success and every failure arm, fallback parent_call chaining via
AgentError.trace_id, the kill-mid-call partial record, redaction before
disk, the Ollama tokens side-channel (CLI turns stay null), parallel-seq
uniqueness, and the never-take-a-turn-down contract.
"""
import concurrent.futures
import json
import os
import shutil
import stat
import tempfile
import unittest

import orchestrator as orch
import traces as traceslib

FAKE_KEY = "sk-ant-api03-" + "x" * 60

SCHEMA_FIELDS = {"schema_version", "trace_id", "parent_call", "ts", "app",
                 "phase", "round", "agent", "producer", "model",
                 "rendered_prompt", "response", "stderr", "exit", "tokens",
                 "duration_s", "status"}


class TraceBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_traces_")
        traceslib._reset_for_tests()
        self._orig_runners = dict(orch.RUNNERS)
        self._orig_avail = orch._agent_available

    def tearDown(self):
        orch.RUNNERS.clear()
        orch.RUNNERS.update(self._orig_runners)
        orch._agent_available = self._orig_avail
        traceslib._reset_for_tests()
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _cfg(self):
        # provider_min_gap_seconds=0: pacing would serialize the parallel
        # test's 24 same-provider calls into minutes of real sleeping.
        return {"agents": {"codex_enabled": True},
                "runtime": {"provider_min_gap_seconds": 0},
                "_app_dir": self.app_dir, "root": self.app_dir,
                "_workflow_target": "app"}

    def traces(self):
        base = os.path.join(self.app_dir, "traces")
        out = []
        for run in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            for f in sorted(os.listdir(os.path.join(base, run))):
                with open(os.path.join(base, run, f), encoding="utf-8") as fh:
                    out.append((run + "/" + f, json.load(fh)))
        return out


class TestLifecycle(TraceBase):
    def test_success_writes_one_complete_trace(self):
        orch.RUNNERS["codex"] = lambda cfg, prompt, timeout: (
            "the answer", "some stderr", 0, "codex-cmd")
        out = orch._call_agent_once(self._cfg(), "app", "design", 2,
                                    "codex", "the rendered prompt")
        self.assertIn("the answer", out)
        traces = self.traces()
        self.assertEqual(len(traces), 1)
        _, rec = traces[0]
        self.assertEqual(set(rec), SCHEMA_FIELDS)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["rendered_prompt"], "the rendered prompt")
        self.assertEqual(rec["response"], "the answer")
        self.assertEqual(rec["exit"], 0)
        self.assertIsNone(rec["parent_call"])
        self.assertIsNone(rec["tokens"], "CLI turns must never fabricate tokens")
        self.assertIsInstance(rec["duration_s"], (int, float))

    def test_empty_output_finalizes_error_and_attaches_trace_id(self):
        orch.RUNNERS["codex"] = lambda cfg, prompt, timeout: ("", "boom", 3, "c")
        with self.assertRaises(orch.AgentError) as ctx:
            orch._call_agent_once(self._cfg(), "app", "design", 1,
                                  "codex", "p")
        (_, rec), = self.traces()
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["exit"], 3)
        self.assertEqual(ctx.exception.trace_id, rec["trace_id"])

    def test_kill_mid_call_leaves_valid_started_record(self):
        def hard_crash(cfg, prompt, timeout):
            raise RuntimeError("simulated kill — no arm catches this")
        orch.RUNNERS["codex"] = hard_crash
        with self.assertRaises(RuntimeError):
            orch._call_agent_once(self._cfg(), "app", "design", 1,
                                  "codex", "partial evidence prompt")
        (_, rec), = self.traces()   # parses as valid JSON or this raises
        self.assertEqual(rec["status"], "started")
        self.assertEqual(rec["rendered_prompt"], "partial evidence prompt")
        self.assertIsNone(rec["response"])

    def test_fallback_chain_links_parent_call(self):
        cfg = self._cfg()
        cfg["model_routing"] = {}
        calls = {"n": 0}

        def flaky(c, prompt, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return ("", "primary down", 1, "c")   # empty output -> error
            return ("rescued answer", "", 0, "c")
        orch.RUNNERS["codex"] = flaky
        orig_steps = orch._fallback_steps
        orch._fallback_steps = lambda c, a: ["gpt-sibling"]
        try:
            out = orch.call_agent(cfg, "app", "design", 1, "codex", "p")
        finally:
            orch._fallback_steps = orig_steps
        self.assertIn("rescued answer", out)
        traces = [rec for _, rec in self.traces()]
        self.assertEqual(len(traces), 2)
        primary, rescue = traces
        self.assertEqual(primary["status"], "error")
        self.assertIsNone(primary["parent_call"])
        self.assertEqual(rescue["status"], "ok")
        self.assertEqual(rescue["parent_call"], primary["trace_id"],
                         "the rescue must chain to its failed primary")


class TestLocalFallbackChain(TraceBase):
    def test_local_rescue_also_links_parent_call(self):
        # The ladder has TWO rescue branches (sibling model vs local:) —
        # each passes parent_call independently; this pins the local one.
        cfg = self._cfg()
        orch.RUNNERS["codex"] = lambda c, p, t: ("", "down", 1, "c")
        orig_local = orch.run_local
        orch.run_local = lambda c, p, t, model=None: ("local rescue", "", 0, "l")
        orig_steps = orch._fallback_steps
        orch._fallback_steps = lambda c, a: ["local:tiny:1b"]
        orig_installed = orch.lmlib.installed_models_cached
        orch.lmlib.installed_models_cached = lambda: ["tiny:1b"]
        try:
            out = orch.call_agent(cfg, "app", "design", 1, "codex", "p")
        finally:
            orch.run_local = orig_local
            orch._fallback_steps = orig_steps
            orch.lmlib.installed_models_cached = orig_installed
        self.assertIn("local rescue", out)
        primary, rescue = [rec for _, rec in self.traces()]
        self.assertEqual(rescue["parent_call"], primary["trace_id"],
                         "the LOCAL rescue must chain to its failed primary")


class TestRedactionAndTokens(TraceBase):
    def test_planted_secret_reaches_zero_trace_bytes(self):
        orch.RUNNERS["codex"] = lambda cfg, prompt, timeout: (
            "reply echoing %s" % FAKE_KEY, "err with %s" % FAKE_KEY, 0, "c")
        orch._call_agent_once(self._cfg(), "app", "design", 1, "codex",
                              "prompt holding %s" % FAKE_KEY)
        base = os.path.join(self.app_dir, "traces")
        for run in os.listdir(base):
            for f in os.listdir(os.path.join(base, run)):
                with open(os.path.join(base, run, f), "rb") as fh:
                    self.assertNotIn(FAKE_KEY.encode(), fh.read(),
                                     "a secret reached a trace byte")

    def test_local_usage_rides_the_side_channel(self):
        def local_runner(cfg, prompt, timeout):
            traceslib.set_last_usage({"prompt_tokens": 41,
                                      "completion_tokens": 7})
            return ("local reply", "", 0, "ollama-cmd")
        orch.RUNNERS["codex"] = local_runner
        orch._call_agent_once(self._cfg(), "app", "design", 1, "codex", "p")
        (_, rec), = self.traces()
        self.assertEqual(rec["tokens"], {"prompt_tokens": 41,
                                         "completion_tokens": 7})

    def test_stale_usage_never_attaches_to_the_next_turn(self):
        traceslib.set_last_usage({"prompt_tokens": 999})   # leftover
        orch.RUNNERS["codex"] = lambda cfg, prompt, timeout: ("ok", "", 0, "c")
        orch._call_agent_once(self._cfg(), "app", "design", 1, "codex", "p")
        (_, rec), = self.traces()
        self.assertIsNone(rec["tokens"],
                          "a stale local count leaked onto a CLI turn")


class TestSeqAndResilience(TraceBase):
    def test_parallel_calls_get_unique_monotonic_seqs(self):
        orch.RUNNERS["codex"] = lambda cfg, prompt, timeout: ("r", "", 0, "c")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(orch._call_agent_once, self._cfg(), "app",
                              "design", i, "codex", "p%d" % i)
                    for i in range(24)]
            for f in futs:
                f.result()
        names = [name for name, _ in self.traces()]
        self.assertEqual(len(names), 24)
        self.assertEqual(len(set(names)), 24, "seq collision under threads")
        seqs = sorted(int(n.split("/")[1].split(".")[0]) for n in names)
        self.assertEqual(seqs, list(range(1, 25)), "seqs must be gap-free")
        ids = [rec["trace_id"] for _, rec in self.traces()]
        self.assertEqual(len(set(ids)), 24)

    def test_unwritable_traces_dir_never_fails_the_turn(self):
        locked = os.path.join(self.app_dir, "traces")
        os.makedirs(locked)
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)   # no write
        self.addCleanup(lambda: os.path.isdir(locked)
                        and os.chmod(locked, stat.S_IRWXU))
        warns = []
        orig_warn = traceslib.on_warn
        traceslib.on_warn = warns.append
        self.addCleanup(setattr, traceslib, "on_warn", orig_warn)
        orch.RUNNERS["codex"] = lambda cfg, prompt, timeout: ("fine", "", 0, "c")
        out = orch._call_agent_once(self._cfg(), "app", "design", 1,
                                    "codex", "p")
        self.assertIn("fine", out, "a trace failure must never fail the turn")
        self.assertTrue(warns, "the failure must be surfaced as one WARN")


if __name__ == "__main__":
    unittest.main()
