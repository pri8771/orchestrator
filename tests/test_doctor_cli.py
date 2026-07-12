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


class TestNoRunnableAgentWarning(_NoExternalTools):
    """process_app checks once per run whether ANY agent is actually runnable,
    so a fully-disabled/uninstalled setup is diagnosed upfront instead of every
    phase-1 turn failing one at a time with no clear cause."""

    def _make_app(self, root, name="myapp"):
        import os
        d = os.path.join(root, name, "initial_prompt")
        os.makedirs(d)
        with open(os.path.join(d, "initial_prompt.md"), "w", encoding="utf-8") as fh:
            fh.write("Build a thing.")

    def test_warns_once_when_nothing_is_runnable(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_app(d)
            cfg = {"root": d, "agents": {"codex_enabled": False, "claude_enabled": False,
                                        "gemini_enabled": False, "ollama_enabled": False},
                  "models": {}, "_resolved": {}, "runtime": {}}
            msgs = []
            with unittest.mock.patch.object(orch, "emit", msgs.append):
                orch.process_app(cfg, d, "myapp")
            self.assertTrue(any("no agent is runnable" in m for m in msgs))
            self.assertTrue(cfg.get("_checked_any_agent_runnable"))

    def test_does_not_warn_when_an_agent_is_available(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_app(d)
            cfg = {"root": d, "agents": {"codex_enabled": True, "claude_enabled": False,
                                        "gemini_enabled": False, "ollama_enabled": False},
                  "models": {}, "_resolved": {}, "runtime": {}}
            msgs = []
            with unittest.mock.patch.object(orch, "which", lambda n: "/usr/bin/codex" if n == "codex" else None), \
                    unittest.mock.patch.object(orch, "emit", msgs.append):
                # A minimal cfg lacks the model-resolution setup a real run
                # would do before reaching here; this test only cares whether
                # the "no agent runnable" warning fired BEFORE that point, so
                # let a downstream KeyError from the incomplete cfg pass.
                try:
                    orch.process_app(cfg, d, "myapp")
                except KeyError:
                    pass
            self.assertFalse(any("no agent is runnable" in m for m in msgs))

    def test_only_checked_once_per_cfg(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_app(d, "app-a")
            self._make_app(d, "app-b")
            cfg = {"root": d, "agents": {"codex_enabled": False, "claude_enabled": False,
                                        "gemini_enabled": False, "ollama_enabled": False},
                  "models": {}, "_resolved": {}, "runtime": {}}
            msgs = []
            with unittest.mock.patch.object(orch, "emit", msgs.append):
                orch.process_app(cfg, d, "app-a")
                orch.process_app(cfg, d, "app-b")
            self.assertEqual(sum("no agent is runnable" in m for m in msgs), 1)


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
