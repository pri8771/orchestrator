import json
import os
import subprocess
import tempfile
import unittest

import localmodels as lm
import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OLLAMA_LIST_OUTPUT = """NAME                    ID              SIZE      MODIFIED
qwen2.5-coder:7b        2b0e2b58e2d8    4.7 GB    2 days ago
deepseek-r1:8b          42182419e950    5.2 GB    4 weeks ago
"""


class TestRegistry(unittest.TestCase):
    def test_shipped_registry_matches_spec(self):
        reg = lm.load_registry(HERE)
        self.assertEqual(reg["schema_version"], 1)
        ids = [m["id"] for m in reg["models"]]
        self.assertEqual(ids, ["qwen3-coder:30b", "glm4:9b", "qwen3:32b",
                               "qwq:32b", "qwen3:8b", "qwen3:14b",
                               "qwen2.5-coder:7b", "devstral:24b", "deepseek-r1:8b",
                               "deepseek-r1:14b", "deepseek-r1:32b",
                               "qwen2.5vl:3b", "gemma3:12b", "gemma3:4b",
                               "phi4:14b", "llama3.2:3b", "mistral:7b",
                               "qwen2.5-coder:14b", "deepseek-coder-v2:16b"])
        # Registry tags must be real ollama pull targets (a fictional tag like
        # glm-5.2:latest fails hard at `ollama pull`). Guard the naming shapes
        # that previously slipped through.
        for bad in ("glm-5.2", "qwen3-max", "qwen3.7", "kimi-k2", "deepseek-v4"):
            self.assertFalse(any(bad in i for i in ids), "fictional tag %r" % bad)
        # Licenses that back a commercial_use=true claim: permissive OSS plus
        # the vendor terms that explicitly permit commercial use.
        commercial_ok_licenses = ("MIT", "Apache-2.0", "Gemma", "Llama", "GLM")
        for m in reg["models"]:
            self.assertEqual(m["runtime"], "ollama")
            self.assertEqual(m["pull_command"], ["ollama", "pull", m["id"]])
            self.assertTrue(m["license"])
            if m["commercial_use"]:
                self.assertTrue(
                    any(license_id in m["license"] for license_id in commercial_ok_licenses),
                    f"{m['id']}: commercial_use=true needs a known commercial-ok license",
                )
            self.assertTrue(m["license_url"].startswith("https://"))
            self.assertGreaterEqual(m["min_ram_gb"], 8)
            # context_tokens is normalized to be present on every entry.
            self.assertIsInstance(m.get("context_tokens"), int)
            self.assertGreater(m["context_tokens"], 0)
            self.assertTrue(m["roles"])

    def test_missing_file_yields_empty_registry(self):
        with tempfile.TemporaryDirectory() as d:
            reg = lm.load_registry(d)
        self.assertEqual(reg, {"schema_version": 1, "models": []})

    def test_malformed_file_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, lm.REGISTRY_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(lm.load_registry(d)["models"], [])
            # wrong shape (models not a list) degrades the same way
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "models": {"nope": 1}}, fh)
            self.assertEqual(lm.load_registry(d)["models"], [])
            # entries without an id are dropped, well-formed ones kept
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1,
                           "models": [{"label": "no id"}, "junk",
                                      {"id": "mistral:7b", "label": "ok"}]}, fh)
            self.assertEqual([m["id"] for m in lm.load_registry(d)["models"]],
                             ["mistral:7b"])


class TestOllamaListParsing(unittest.TestCase):
    def test_parses_table_skipping_header(self):
        self.assertEqual(lm.parse_ollama_list(OLLAMA_LIST_OUTPUT),
                         ["qwen2.5-coder:7b", "deepseek-r1:8b"])

    def test_empty_and_garbage_input(self):
        self.assertEqual(lm.parse_ollama_list(""), [])
        self.assertEqual(lm.parse_ollama_list(None), [])
        self.assertEqual(lm.parse_ollama_list("\n\n  \n"), [])

    def test_installed_models_uses_injected_runner(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            return OLLAMA_LIST_OUTPUT, "", 0
        self.assertEqual(lm.installed_models(run=fake_run),
                         ["qwen2.5-coder:7b", "deepseek-r1:8b"])
        self.assertEqual(calls, [["ollama", "list"]])

    def test_installed_models_never_raises(self):
        def missing(cmd, timeout=None):
            raise FileNotFoundError("no ollama on PATH")
        self.assertEqual(lm.installed_models(run=missing), [])

        def hangs(cmd, timeout=None):
            raise subprocess.TimeoutExpired(cmd, timeout)
        self.assertEqual(lm.installed_models(run=hangs), [])

        def fails(cmd, timeout=None):
            return "", "server not running", 1
        self.assertEqual(lm.installed_models(run=fails), [])


class TestRosterGating(unittest.TestCase):
    """enabled_agents() includes/excludes local identities per config."""

    def cfg(self, **kw):
        c = {"agents": {"ollama_enabled": True}, "models": {"ollama": "qwen2.5-coder:7b"},
             "runtime": {}}
        c.update(kw)
        return c

    def test_off_by_default(self):
        self.assertEqual(orch.enabled_agents({}), ["codex", "claude", "gemini"])

    def test_enabled_with_model_joins_last(self):
        self.assertEqual(orch.enabled_agents(self.cfg()),
                         ["codex", "claude", "gemini",
                          "local:qwen2.5-coder:7b"])

    def test_enabled_with_ollama_roster(self):
        c = self.cfg()
        c["models"]["ollama_roster"] = "qwen2.5-coder:7b;deepseek-r1:8b, qwen2.5-coder:7b"
        self.assertEqual(orch.enabled_agents(c),
                         ["codex", "claude", "gemini",
                          "local:qwen2.5-coder:7b", "local:deepseek-r1:8b"])

    def test_can_skip_uninstalled_roster_models(self):
        c = self.cfg()
        c["models"]["ollama_roster"] = "qwen2.5-coder:7b,glm4:9b,gemma3:4b"
        c["runtime"]["skip_uninstalled_local_models"] = True
        c["_installed_ollama_models"] = ["gemma3:4b"]
        self.assertEqual(orch.enabled_agents(c),
                         ["codex", "claude", "gemini", "local:gemma3:4b"])
        self.assertTrue(c.get("_noted_ollama_uninstalled_skip"))

    def test_uses_resolved_roster_when_present(self):
        c = self.cfg()
        c["_resolved"] = {"ollama_roster": ["glm4:9b", "qwq:32b"]}
        c["models"]["ollama_roster"] = "qwen3-coder:30b"
        self.assertEqual(orch.enabled_agents(c),
                         ["codex", "claude", "gemini",
                          "local:glm4:9b", "local:qwq:32b"])

    def test_enabled_without_model_is_excluded(self):
        c = self.cfg()
        c["models"]["ollama"] = ""
        self.assertEqual(orch.enabled_agents(c), ["codex", "claude", "gemini"])
        c["models"]["ollama"] = "   "
        self.assertEqual(orch.enabled_agents(c), ["codex", "claude", "gemini"])

    def test_disabled_with_model_is_excluded(self):
        c = self.cfg()
        c["agents"]["ollama_enabled"] = False
        self.assertEqual(orch.enabled_agents(c), ["codex", "claude", "gemini"])

    def test_sprint_budget_excludes_local_by_default(self):
        c = self.cfg()
        c["_budget"] = {"time_budget_minutes": 55}
        self.assertEqual(orch.enabled_agents(c), ["codex", "claude", "gemini"])
        # note emitted once, then remembered on cfg
        self.assertTrue(c.get("_noted_ollama_sprint_skip"))

    def test_sprint_opt_in_keeps_local(self):
        c = self.cfg()
        c["_budget"] = {"time_budget_minutes": 55}
        c["runtime"]["local_models_in_sprints"] = True
        self.assertEqual(orch.enabled_agents(c),
                         ["codex", "claude", "gemini",
                          "local:qwen2.5-coder:7b"])

    def test_cloud_agents_unaffected_by_budget(self):
        c = {"_budget": {"time_budget_minutes": 10}}
        self.assertEqual(orch.enabled_agents(c), ["codex", "claude", "gemini"])


class TestCoordinatorNeverLocal(unittest.TestCase):
    def setUp(self):
        self._avail = orch._agent_available

    def tearDown(self):
        orch._agent_available = self._avail

    def test_cloud_agent_wins_when_available(self):
        orch._agent_available = lambda a: True
        self.assertEqual(orch._pick_coordinator({}, ["codex", "claude", "gemini", "local:qwen2.5-coder:7b"]),
                         "claude")
        self.assertEqual(orch._pick_coordinator({}, ["gemini", "local:qwen2.5-coder:7b"]), "gemini")

    def test_enabled_cloud_agent_wins_even_if_uninstalled(self):
        # Only the local runtime is actually invokable, but a cloud agent is
        # still enabled: it must still out-rank the local model as coordinator.
        orch._agent_available = lambda a: a == "local:qwen2.5-coder:7b"
        self.assertEqual(orch._pick_coordinator({}, ["claude", "local:qwen2.5-coder:7b"]), "claude")

    def test_local_only_roster_may_coordinate(self):
        # No cloud agent enabled at all — the local model is the only choice.
        orch._agent_available = lambda a: a == "local:qwen2.5-coder:7b"
        self.assertEqual(orch._pick_coordinator({}, ["local:qwen2.5-coder:7b"]),
                         "local:qwen2.5-coder:7b")

    def test_ollama_not_in_coordinator_preference(self):
        self.assertNotIn("ollama", orch.COORDINATOR_PREFERENCE)
        self.assertEqual(orch.AGENT_ORDER[-1], "ollama")


class TestRunnerDispatch(unittest.TestCase):
    def test_ollama_runner_registered_and_display(self):
        self.assertIs(orch.resolve_runner("ollama"), orch.run_ollama)
        self.assertEqual(orch.DISPLAY["ollama"], "Local (Ollama)")
        self.assertEqual(orch.SIGNATURE["ollama"], "From Local (Ollama)")

    def test_run_ollama_pipes_prompt_on_stdin(self):
        seen = {}

        def fake_run(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            seen.update(cmd=cmd, cwd=cwd, timeout=timeout,
                        heartbeat=heartbeat, input_text=input_text)
            return "local says hi", "", 0
        orig = orch._run_subprocess
        orch._run_subprocess = fake_run
        try:
            cfg = {"models": {"ollama": "qwen2.5-coder:7b"},
                   "runtime": {"agent_no_output_heartbeat_seconds": 600,
                               "isolate_agent_cwd": True}}
            out, err, code, cmd = orch.run_ollama(cfg, "PROMPT BODY", 120)
        finally:
            orch._run_subprocess = orig
        self.assertEqual(seen["cmd"], ["ollama", "run", "qwen2.5-coder:7b"])
        self.assertEqual(seen["input_text"], "PROMPT BODY")   # stdin, not argv
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(seen["heartbeat"], 600)
        self.assertEqual((out, err, code), ("local says hi", "", 0))
        self.assertIn("prompt on stdin", cmd)

    def test_run_ollama_without_model_is_clean_error(self):
        with self.assertRaises(orch.AgentError):
            orch.run_ollama({"models": {"ollama": ""}}, "hi", 5)

    def test_run_capture_feeds_stdin(self):
        # Real subprocess, no ollama involved: cat must echo the piped prompt
        # back on both the fast path and the heartbeat path.
        import procutil
        out, err, code = procutil.run_capture(["cat"], timeout=10,
                                              input_text="hello stdin")
        self.assertEqual((out, code), ("hello stdin", 0))
        out, err, code = procutil.run_capture(["cat"], timeout=10, heartbeat=5,
                                              input_text="hello heartbeat")
        self.assertEqual((out, code), ("hello heartbeat", 0))


class TestDoctorLocalModels(unittest.TestCase):
    """--doctor --json exposes the local_models block; probes are monkeypatched
    so this passes with or without ollama installed (no real ollama calls)."""

    def setUp(self):
        self._server, self._installed = lm.server_running, lm.installed_models
        self._which = orch.which
        lm.server_running = lambda timeout=3: True
        lm.installed_models = lambda run=None: ["qwen2.5-coder:7b"]
        orch.which = lambda name: None   # no real version probes in tests

    def tearDown(self):
        lm.server_running, lm.installed_models = self._server, self._installed
        orch.which = self._which

    def test_report_shape(self):
        rep = lm.report(HERE, "qwen2.5-coder:7b")
        self.assertEqual(set(rep), {"server_running", "selected",
                                    "selected_installed", "registry"})
        self.assertTrue(rep["server_running"])
        self.assertEqual(rep["selected"], "qwen2.5-coder:7b")
        self.assertTrue(rep["selected_installed"])
        for m in rep["registry"]:
            self.assertIn("id", m)
            self.assertIn("label", m)
            self.assertIn("installed", m)
            self.assertIn("license", m)
            self.assertIn("commercial_use", m)
        by_id = {m["id"]: m for m in rep["registry"]}
        self.assertTrue(by_id["qwen2.5-coder:7b"]["installed"])
        self.assertFalse(by_id["mistral:7b"]["installed"])
        self.assertEqual(by_id["qwen3-coder:30b"]["label"], "Best Local Coding Agent")
        self.assertEqual(by_id["qwen3-coder:30b"]["license"], "Apache-2.0")

    def test_no_selection(self):
        rep = lm.report(HERE, "")
        self.assertEqual(rep["selected"], "")
        self.assertFalse(rep["selected_installed"])

    def test_preflight_report_includes_local_models(self):
        cfg = {"root": tempfile.gettempdir(),
               "models": {"ollama": "mistral:7b"}, "_resolved": {}}
        rep = orch.preflight_report(cfg)
        lm_block = rep["local_models"]
        self.assertEqual(set(lm_block), {"server_running", "selected",
                                         "selected_installed", "registry"})
        self.assertTrue(lm_block["server_running"])
        self.assertEqual(lm_block["selected"], "mistral:7b")
        self.assertFalse(lm_block["selected_installed"])   # not in fake list
        self.assertEqual(len(lm_block["registry"]),
                         len(lm.load_registry(HERE)["models"]))
        # and the whole report stays JSON-serializable for --doctor --json
        json.dumps(rep)


if __name__ == "__main__":
    unittest.main()
