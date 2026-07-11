"""Tests for the gemini startup probe (detect_gemini_available):

- one tiny headless `gemini -p` call decides availability; exit 41 / error
  output / nonzero exit -> unavailable with a human reason
- the verdict is cached 4h in .gemini_probe.json (like the codex probe)
- resolve_models auto-disables gemini for the run on a failed probe
  (cfg["_gemini_disabled_reason"]) and enabled_agents drops it from the
  roster — instead of every turn failing
- agy-only and no-CLI environments short-circuit without a live probe
"""
import json
import os
import shutil
import tempfile
import unittest

import orchestrator as orch


class _ProbeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orch_gprobe_")
        self._orig_here = orch.HERE
        self._orig_which = orch.which
        self._orig_run = orch.procutil.run_capture
        orch.HERE = self.tmp   # cache file lands in the tmp dir

    def tearDown(self):
        orch.HERE = self._orig_here
        orch.which = self._orig_which
        orch.procutil.run_capture = self._orig_run
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wire(self, clis=("gemini",), out="", err="", code=0):
        orch.which = lambda name: ("/usr/bin/%s" % name) if name in clis else None
        calls = []

        def fake_run(cmd, cwd=None, timeout=None, env=None, heartbeat=None,
                     input_text=None):
            calls.append(list(cmd))
            return out, err, code

        orch.procutil.run_capture = fake_run
        return calls

    def _cache(self):
        with open(os.path.join(self.tmp, ".gemini_probe.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)


class TestDetectGeminiAvailable(_ProbeBase):
    def test_success_probe(self):
        calls = self._wire(out="OK", code=0)
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "gemini")
        self.assertTrue(self._cache()["ok"])

    def test_exit_41_is_unavailable_with_reason(self):
        self._wire(out="", err="", code=41)
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertFalse(ok)
        self.assertIn("41", reason)
        c = self._cache()
        self.assertFalse(c["ok"])
        self.assertIn("41", c["reason"])

    def test_tty_error_fingerprint_is_unavailable(self):
        self._wire(out="CLI error: could not open tty", code=0)
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertFalse(ok)
        self.assertIn("tty", reason.lower())

    def test_nonzero_exit_is_unavailable(self):
        self._wire(out="", err="IneligibleTier: OAuth deprecated", code=1)
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertFalse(ok)
        self.assertIn("exit 1", reason)

    def test_cached_verdict_skips_the_probe(self):
        self._wire(out="", code=41)
        orch.detect_gemini_available({"models": {}})   # writes the cache

        def boom(*a, **k):
            raise AssertionError("probe must not run while cached")

        orch.procutil.run_capture = boom
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertFalse(ok)
        self.assertIn("41", reason)

    def test_stale_cache_reprobes(self):
        self._wire(out="", code=41)
        orch.detect_gemini_available({"models": {}})
        c = self._cache()
        c["ts"] -= 5 * 3600   # older than the 4h TTL
        with open(os.path.join(self.tmp, ".gemini_probe.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(c, fh)
        calls = self._wire(out="OK", code=0)
        ok, _ = orch.detect_gemini_available({"models": {}})
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_agy_only_defers_to_runtime(self):
        calls = self._wire(clis=("agy",))
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertTrue(ok)
        self.assertEqual(calls, [])   # no live probe for agy

    def test_no_cli_at_all(self):
        calls = self._wire(clis=())
        ok, reason = orch.detect_gemini_available({"models": {}})
        self.assertFalse(ok)
        self.assertIn("no gemini/agy CLI", reason)
        self.assertEqual(calls, [])

    def test_valid_model_id_passed_to_probe(self):
        calls = self._wire(out="OK", code=0)
        orch.detect_gemini_available(
            {"models": {"gemini_fallback": "gemini-3.5-flash"}})
        self.assertIn("-m", calls[0])
        self.assertIn("gemini-3.5-flash", calls[0])


class TestResolveModelsAutoDisable(_ProbeBase):
    def _cfg(self):
        return {
            "models": {"claude": "sonnet", "codex": "gpt-5",
                       "gemini_fallback": "gemini-3.5-flash"},
            "agents": {"codex_enabled": True, "claude_enabled": True,
                       "gemini_enabled": True},
        }

    def test_failed_probe_disables_gemini_for_the_run(self):
        self._wire(out="", code=41)
        cfg = self._cfg()
        orch.resolve_models(cfg)
        self.assertIn("41", cfg.get("_gemini_disabled_reason", ""))
        self.assertNotIn("gemini", orch.enabled_agents(cfg))
        self.assertIn("claude", orch.enabled_agents(cfg))

    def test_passing_probe_keeps_gemini_enabled(self):
        self._wire(out="OK", code=0)
        cfg = self._cfg()
        orch.resolve_models(cfg)
        self.assertNotIn("_gemini_disabled_reason", cfg)
        self.assertIn("gemini", orch.enabled_agents(cfg))

    def test_probe_skipped_when_gemini_disabled_in_config(self):
        def boom(*a, **k):
            raise AssertionError("probe must not run when config-disabled")

        self._wire()
        orch.procutil.run_capture = boom
        cfg = self._cfg()
        cfg["agents"]["gemini_enabled"] = False
        orch.resolve_models(cfg)
        self.assertNotIn("_gemini_disabled_reason", cfg)
        self.assertNotIn("gemini", orch.enabled_agents(cfg))


if __name__ == "__main__":
    unittest.main()
