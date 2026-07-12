"""Smoke tests for the documented CLI entrypoints: --doctor, --doctor --json,
--search-models. These are user-facing surfaces (and GUI onboarding consumers)
that had no test; all external probes are stubbed so nothing shells out.
"""
import json
import tempfile
import unittest
import unittest.mock

import orchestrator as orch
import localmodels as lm


class _NoExternalTools(unittest.TestCase):
    """Patch away every real CLI/subprocess probe so the doctor paths run
    hermetically with or without codex/claude/ollama/xcrun installed."""

    def setUp(self):
        self._which = orch.which
        self._server = lm.server_running
        self._installed = lm.installed_models
        self._quiet = orch._QUIET
        orch.which = lambda name: None          # nothing found on PATH
        lm.server_running = lambda timeout=3: False
        lm.installed_models = lambda run=None: []
        orch._QUIET = True                       # silence emit() during the test

    def tearDown(self):
        orch.which = self._which
        lm.server_running = self._server
        lm.installed_models = self._installed
        orch._QUIET = self._quiet

    def _cfg(self):
        return {"root": tempfile.gettempdir(), "models": {"ollama": ""},
                "agents": {}, "_resolved": {}}


class TestDoctorText(_NoExternalTools):
    def test_doctor_runs_without_raising(self):
        orch.doctor(self._cfg())   # prints (suppressed) — must not raise


class TestDoctorJson(_NoExternalTools):
    def test_preflight_report_shape_and_serializable(self):
        rep = orch.preflight_report(self._cfg())
        self.assertIsInstance(rep, dict)
        self.assertIn("tools", rep)
        self.assertIn("local_models", rep)
        # --doctor --json must emit valid JSON.
        json.dumps(rep)

    def test_tools_block_reports_absent_cleanly(self):
        rep = orch.preflight_report(self._cfg())
        for name, info in rep["tools"].items():
            self.assertIn("present", info)
            self.assertFalse(info["present"], name)   # patched which -> None


class TestSearchModels(_NoExternalTools):
    def test_search_models_offline_degrades_to_curated(self):
        # HF search is network; inject a fetcher that fails so we exercise the
        # offline path — curated registry hits must still come through.
        def offline(_url, timeout=10):
            raise OSError("offline")
        res = lm.search_remote("qwen coder", fetch=offline, here=orch.HERE)
        self.assertIsInstance(res, dict)
        self.assertEqual(res["query"], "qwen coder")
        self.assertTrue(any(r["source"] == "curated" for r in res["results"]))


if __name__ == "__main__":
    unittest.main()
