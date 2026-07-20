"""V3 6.1: capability descriptors — the machine-readable truth about what each
agent id can do (streams / token_usage / effort_control / session_resume),
exported through --doctor --json so no GUI surface promises a feature the
backend lacks. Values are pinned against runner reality, not just declared
(§23 verify-don't-self-report).
"""
import json
import os
import tempfile
import unittest

import localmodels as lm
import orchestrator as orch

FIELDS = ("streams", "token_usage", "effort_control", "session_resume")


class TestRegistryCompleteness(unittest.TestCase):
    def test_every_runner_id_has_a_descriptor(self):
        for aid in orch.RUNNERS:
            self.assertIn(aid, orch.AGENT_CAPABILITIES, aid)

    def test_every_descriptor_has_exactly_the_four_fields(self):
        for aid, caps in orch.AGENT_CAPABILITIES.items():
            self.assertEqual(set(caps), set(FIELDS), aid)
        for prefix, caps in orch.DYNAMIC_CAPABILITY_PREFIXES.items():
            self.assertEqual(set(caps), set(FIELDS), prefix)

    def test_only_api_prefix_claims_streams_or_token_usage(self):
        # No CLI runner streams or reports token usage on the formal contract;
        # only the api: prefix rule claims token_usage (6.2 — real usage via
        # the traces side-channel) and SSE preview lifecycle. A True anywhere
        # else would be a decorative control (§16).
        for aid, caps in orch.AGENT_CAPABILITIES.items():
            self.assertFalse(caps["streams"], aid)
            self.assertFalse(caps["token_usage"], aid)
        self.assertFalse(orch.DYNAMIC_CAPABILITY_PREFIXES["local:"]["streams"])
        self.assertTrue(orch.DYNAMIC_CAPABILITY_PREFIXES["api:"]["streams"])


class TestRunnerRealityPins(unittest.TestCase):
    """Descriptor values asserted against the runner behavior they describe."""

    def test_effort_control_matches_runners(self):
        # run_codex passes model_reasoning_effort; run_claude passes --effort.
        self.assertTrue(orch.AGENT_CAPABILITIES["codex"]["effort_control"])
        self.assertTrue(orch.AGENT_CAPABILITIES["claude"]["effort_control"])
        # run_gemini has no effort flag; run_ollama's CLI path ignores
        # ollama_reasoning (only run_local honors it).
        self.assertFalse(orch.AGENT_CAPABILITIES["gemini"]["effort_control"])
        self.assertFalse(orch.AGENT_CAPABILITIES["ollama"]["effort_control"])

    def test_codex_session_resume_is_build_only_never_true(self):
        # call_agent_sessioned: `codex exec resume` always runs workspace-write,
        # so codex is only resumed under _allow_writes. A bare True would lie.
        self.assertEqual(
            orch.AGENT_CAPABILITIES["codex"]["session_resume"], "build_only")
        self.assertIsNot(orch.AGENT_CAPABILITIES["codex"]["session_resume"], True)

    def test_claude_resumes_everywhere_gemini_ollama_never(self):
        self.assertIs(orch.AGENT_CAPABILITIES["claude"]["session_resume"], True)
        self.assertIs(orch.AGENT_CAPABILITIES["gemini"]["session_resume"], False)
        self.assertIs(orch.AGENT_CAPABILITIES["ollama"]["session_resume"], False)

    def test_session_resume_pins_call_agent_sessioned_gate(self):
        # The descriptor must agree with the actual sessionable gate: claude in
        # any phase, codex only when _allow_writes is set.
        for aid, caps in orch.AGENT_CAPABILITIES.items():
            for writes in (False, True):
                gated = aid == "claude" or (aid == "codex" and writes)
                expected = (caps["session_resume"] is True
                            or (caps["session_resume"] == "build_only" and writes))
                self.assertEqual(gated, expected, (aid, writes))


class TestDynamicResolution(unittest.TestCase):
    def test_static_ids_resolve(self):
        for aid in orch.RUNNERS:
            self.assertEqual(orch.resolve_capabilities(aid),
                             orch.AGENT_CAPABILITIES[aid], aid)

    def test_local_prefix(self):
        caps = orch.resolve_capabilities("local:qwen3:8b")
        self.assertEqual(caps, {"streams": False, "token_usage": False,
                                "effort_control": True, "session_resume": False})

    def test_api_prefix(self):
        caps = orch.resolve_capabilities("api:anthropic:claude-sonnet-5")
        self.assertEqual(caps, {"streams": True, "token_usage": True,
                                "effort_control": False, "session_resume": False})

    def test_unknown_and_degenerate_ids_never_raise(self):
        allfalse = {"streams": False, "token_usage": False,
                    "effort_control": False, "session_resume": False}
        for bogus in ("nope", "", None, 7, "local:", "api:", ["claude"]):
            self.assertEqual(orch.resolve_capabilities(bogus), allfalse, bogus)

    def test_returned_dict_is_a_copy(self):
        caps = orch.resolve_capabilities("claude")
        caps["effort_control"] = False
        self.assertTrue(orch.AGENT_CAPABILITIES["claude"]["effort_control"])
        self.assertTrue(orch.resolve_capabilities("claude")["effort_control"])


class TestDoctorExport(unittest.TestCase):
    """--doctor --json carries the block (probes stubbed, hermetic)."""

    def setUp(self):
        self._which = orch.which
        self._server = lm.server_running
        self._installed = lm.installed_models
        self._quiet = orch._QUIET
        orch.which = lambda name: None
        lm.server_running = lambda timeout=3: False
        lm.installed_models = lambda run=None: []
        orch._QUIET = True
        lm._INSTALLED_CACHE.update(ts=0.0, models=[])

    def tearDown(self):
        orch.which = self._which
        lm.server_running = self._server
        lm.installed_models = self._installed
        orch._QUIET = self._quiet
        lm._INSTALLED_CACHE.update(ts=0.0, models=[])

    def _cfg(self):
        return {"root": tempfile.gettempdir(), "models": {"ollama": ""},
                "agents": {}, "_resolved": {}}

    def test_doctor_json_contains_capabilities_block(self):
        rep = orch.preflight_report(self._cfg())
        block = rep["agent_capabilities"]
        self.assertEqual(set(block), {"agents", "dynamic_prefixes"})
        self.assertEqual(set(block["agents"]), set(orch.RUNNERS))
        self.assertEqual(set(block["dynamic_prefixes"]), {"local:", "api:"})
        json.dumps(rep)   # stays --doctor --json serializable

    def test_existing_doctor_keys_survive(self):
        # Adding the block must not remove or rename anything the GUI reads.
        rep = orch.preflight_report(self._cfg())
        for key in ("schema_version", "tools", "resolved_models",
                    "local_models", "model_routing"):
            self.assertIn(key, rep)


if __name__ == "__main__":
    unittest.main()
