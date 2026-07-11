import unittest
import resilience as r


class TestCircuitBreaker(unittest.TestCase):
    def test_trips_after_threshold(self):
        h = r.new_health()
        r.record_failure(h, "crash", now=1000.0)
        self.assertEqual(h["status"], "up")          # 1 failure: still up
        r.record_failure(h, "crash", now=1001.0)
        self.assertEqual(h["status"], "down")         # 2nd trips it
        self.assertEqual(h["retry_after"], 1001.0 + r.DEFAULT_COOLDOWN)

    def test_rate_limit_long_cooldown(self):
        h = r.new_health()
        r.record_failure(h, "rate_limit", now=0.0)
        r.record_failure(h, "rate_limit", now=0.0)
        self.assertEqual(h["retry_after"], r.RATE_LIMIT_COOLDOWN)

    def test_backoff_doubles_and_caps(self):
        h = r.new_health()
        for i in range(8):
            r.record_failure(h, "crash", now=0.0)
        self.assertLessEqual(h["retry_after"], r.MAX_COOLDOWN)

    def test_success_resets(self):
        h = r.new_health()
        r.record_failure(h, "crash", now=0.0)
        r.record_failure(h, "crash", now=0.0)
        r.record_success(h)
        self.assertEqual(h["status"], "up")
        self.assertEqual(h["consecutive_failures"], 0)
        self.assertIsNone(h["retry_after"])

    def test_in_cooldown_and_probe(self):
        h = r.new_health()
        r.record_failure(h, "crash", now=100.0)
        r.record_failure(h, "crash", now=100.0)
        ra = h["retry_after"]
        self.assertTrue(r.in_cooldown(h, now=ra - 1))
        self.assertFalse(r.in_cooldown(h, now=ra + 1))
        self.assertFalse(r.due_for_probe(h, now=ra - 1))
        self.assertTrue(r.due_for_probe(h, now=ra + 1))

    def test_classify(self):
        self.assertEqual(r.classify_failure(124, ""), "timeout")
        self.assertEqual(r.classify_failure(1, "You hit the rate limit"), "rate_limit")
        self.assertEqual(r.classify_failure(1, "usage limit reached"), "rate_limit")
        self.assertEqual(r.classify_failure(1, "invalid json in block"), "malformed_output")
        self.assertEqual(r.classify_failure(1, "segfault"), "crash")


class TestLocalAdapter(unittest.TestCase):
    def test_resolve_runner_local_and_cloud(self):
        import orchestrator as orch
        self.assertIs(orch.resolve_runner("codex"), orch.run_codex)
        fn = orch.resolve_runner("local:llama3.1:8b")
        self.assertTrue(callable(fn))

    def test_local_unreachable_is_clean_error(self):
        # With no Ollama server assumed at an unused port path, run_local must
        # return a clean error tuple, never raise. (Uses the real default URL;
        # if a server happens to run, code 0 is also acceptable.)
        import orchestrator as orch
        out, err, code, cmd = orch.run_local({}, "hi", timeout=3, model="nope-not-real:0")
        self.assertIsInstance(out, str)
        self.assertIsInstance(code, int)
        self.assertTrue(cmd.startswith("ollama:generate"))


if __name__ == "__main__":
    unittest.main()
