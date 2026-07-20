"""V3 6.5: schema-constrained decoding for local models — contract schema
compilation, run_local's Ollama `format` passthrough, the repair-turn
set/clear discipline, and bare-JSON fence-wrapping that keeps downstream
block parsers unchanged.
"""
import json
import unittest
import unittest.mock

import orchestrator as orch
import schemas as schemalib
import turncontext as tcxlib


class TestSchemaCompile(unittest.TestCase):
    def test_fields_compile_to_ollama_schema(self):
        schema = schemalib.contract_to_json_schema(["tasks"])
        self.assertEqual(schema, {"type": "object", "properties": {"tasks": {}},
                                  "required": ["tasks"],
                                  "additionalProperties": True})

    def test_empty_or_none_means_unconstrained(self):
        self.assertIsNone(schemalib.contract_to_json_schema(None))
        self.assertIsNone(schemalib.contract_to_json_schema([]))
        self.assertIsNone(schemalib.contract_to_json_schema(["", None]))


class TestRunLocalFormat(unittest.TestCase):
    def _call(self, cfg):
        captured = {}

        class _Resp:
            def read(self):
                return json.dumps({"response": "ok"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured.update(json.loads(req.data.decode()))
            return _Resp()

        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            orch.run_local(cfg, "hi", 10, model="m1")
        return captured

    def test_format_rides_the_body_when_armed(self):
        cfg = {}
        tcxlib.TurnContext(cfg).structured_format = {
            "schema": {"type": "object", "required": ["tasks"],
                       "properties": {"tasks": {}},
                       "additionalProperties": True},
            "fence_tag": "tasks-json", "required_fields": ["tasks"]}
        body = self._call(cfg)
        self.assertEqual(body["format"]["required"], ["tasks"])

    def test_no_stash_means_no_format_field(self):
        self.assertNotIn("format", self._call({}))

    def test_malformed_stash_never_constrains(self):
        cfg = {}
        tcxlib.TurnContext(cfg).structured_format = {"schema": "not-a-dict"}
        self.assertNotIn("format", self._call(cfg))


class TestRepairSetupAndWrap(unittest.TestCase):
    def setUp(self):
        self._quiet = orch._QUIET
        orch._QUIET = True

    def tearDown(self):
        orch._QUIET = self._quiet

    def test_local_candidate_arms_and_stashes(self):
        cfg = {}
        self.assertTrue(orch._structured_repair_setup(cfg, "local:qwen3", "tasks"))
        sf = tcxlib.TurnContext(cfg).structured_format
        self.assertEqual(sf["fence_tag"], "tasks-json")
        self.assertEqual(sf["schema"]["required"], ["tasks"])

    def test_cli_and_cloud_candidates_never_arm(self):
        for cand in ("ollama", "claude", "codex", "api:openai:g"):
            cfg = {}
            self.assertFalse(orch._structured_repair_setup(cfg, cand, "tasks"),
                             cand)
            self.assertIsNone(tcxlib.TurnContext(cfg).structured_format, cand)

    def test_unknown_kind_never_arms(self):
        cfg = {}
        self.assertFalse(orch._structured_repair_setup(cfg, "local:m", "mystery"))
        self.assertIsNone(tcxlib.TurnContext(cfg).structured_format)

    def test_wrap_produces_a_parseable_fenced_block(self):
        bare = json.dumps({"tasks": [{"id": "T-1", "title": "x",
                                      "owner_lane": "primary_ui",
                                      "files": [], "status": "pending"}]})
        wrapped = orch._wrap_constrained_repair(bare, "tasks")
        self.assertTrue(wrapped.startswith("```tasks-json"))
        errs = []
        blocks = schemalib.extract_structured_blocks(
            wrapped, "tasks-json", on_error=errs.append)
        self.assertEqual(errs, [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["tasks"][0]["id"], "T-1")

    def test_wrap_passes_garbage_through_unchanged(self):
        # Never fabricate a block around a non-conforming response — the
        # caller's normal repair loop handles it (§13.2).
        for resp in ("not json", "[1,2]", "", None):
            self.assertEqual(orch._wrap_constrained_repair(resp, "tasks"), resp)


class TestRepairContractEndToEnd(unittest.TestCase):
    def setUp(self):
        self._quiet = orch._QUIET
        orch._QUIET = True

    def tearDown(self):
        orch._QUIET = self._quiet

    def test_local_repair_turn_is_constrained_wrapped_and_cleared(self):
        cfg = {}
        seen = {}

        def fake_call_agent(cfg_, app, key, rnd, cand, prompt):
            seen["stash"] = tcxlib.TurnContext(cfg_).structured_format
            return json.dumps({"tasks": []})

        with unittest.mock.patch.object(orch, "_coordinator_candidates",
                                        lambda *a, **k: ["local:qwen3"]), \
                unittest.mock.patch.object(orch, "call_agent",
                                           fake_call_agent), \
                unittest.mock.patch.object(orch, "append_md",
                                           lambda *a, **k: None), \
                unittest.mock.patch.object(orch.msglib, "append_message",
                                           lambda *a, **k: None):
            transcript, resp = orch._repair_contract(
                cfg, "appx", "/tmp/nowhere", "tech_specs", "claude",
                ["local:qwen3"], "md", "", "tasks", "fix it")
        # The turn saw the armed stash; the response came back fence-wrapped;
        # the stash is CLEARED afterward (a stale schema must never leak).
        self.assertEqual(seen["stash"]["fence_tag"], "tasks-json")
        self.assertIn("```tasks-json", resp)
        self.assertIsNone(tcxlib.TurnContext(cfg).structured_format)

    def test_failed_candidate_still_clears_the_stash(self):
        cfg = {}

        def raising_call_agent(*a, **k):
            raise orch.AgentError("dead")

        with unittest.mock.patch.object(orch, "_coordinator_candidates",
                                        lambda *a, **k: ["local:qwen3"]), \
                unittest.mock.patch.object(orch, "call_agent",
                                           raising_call_agent):
            transcript, resp = orch._repair_contract(
                cfg, "appx", "/tmp/nowhere", "tech_specs", "claude",
                ["local:qwen3"], "md", "", "tasks", "fix it")
        self.assertIsNone(resp)
        self.assertIsNone(tcxlib.TurnContext(cfg).structured_format)


if __name__ == "__main__":
    unittest.main()
