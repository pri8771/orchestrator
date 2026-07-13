import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import localmodels as lm
import modelrouting as mr
import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_routing(d, payload):
    path = os.path.join(d, mr.ROUTING_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh)
    return path


class TestLoadRouting(unittest.TestCase):
    def test_missing_file_yields_default_on(self):
        with tempfile.TemporaryDirectory() as d:
            r = mr.load_routing(d)
        self.assertTrue(r["enabled"])
        self.assertTrue(r["fallback"]["cloud_to_local"])
        self.assertEqual(r["phases"], {})

    def test_string_false_round_trips_as_falsy_not_bool_coerced(self):
        # bool("false") is True — a hand-edited/agent-emitted JSON-as-string
        # "false" must not flip enabled/cloud_to_local back on.
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {
                "enabled": "false",
                "fallback": {"cloud_to_local": "false"},
            })
            r = mr.load_routing(d)
        self.assertIs(r["enabled"], False)
        self.assertIs(r["fallback"]["cloud_to_local"], False)

    def test_malformed_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, "{not json")
            self.assertEqual(mr.load_routing(d)["phases"], {})
            write_routing(d, {"phases": ["not", "a", "dict"]})
            self.assertEqual(mr.load_routing(d)["phases"], {})

    def test_valid_fields_kept_junk_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {
                "enabled": True,
                "fallback": {"cloud_to_local": False, "local_model": "qwen2.5-coder:7b"},
                "phases": {
                    "build_coordination": {"claude": "claude-opus-4-8",
                                           "claude_reasoning": "high",
                                           "codex_reasoning": "high",
                                           "evil": "rm -rf",         # unknown field
                                           "codex": "bad\nvalue"},   # control char
                    "nope": "not a dict",
                    "empty": {"claude": "   "},
                },
            })
            r = mr.load_routing(d)
        self.assertFalse(r["fallback"]["cloud_to_local"])
        self.assertEqual(r["fallback"]["local_model"], "qwen2.5-coder:7b")
        self.assertEqual(r["phases"], {"build_coordination":
                                       {"claude": "claude-opus-4-8",
                                        "claude_reasoning": "high",
                                        "codex_reasoning": "high"}})

    def test_shipped_routing_file_parses(self):
        r = mr.load_routing(HERE)
        self.assertIn("fallback", r)
        self.assertTrue(mr.fallback_enabled(r))


class TestLoadRoutingForApp(unittest.TestCase):
    """Per-project model_routing.json overlaid on the fleet file (DESIGN-
    NATIVE-PRO.md #5.7) — the engine side of the GUI's Plan-tab grid."""

    def setUp(self):
        self.fleet = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.fleet, True)
        self.app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.app, True)
        write_routing(self.fleet, {
            "fallback": {"cloud_to_local": False, "local_model": "qwen2.5-coder:7b"},
            "phases": {"build_coordination": {"claude": "claude-sonnet-5",
                                              "codex_reasoning": "low"},
                       "tech_specs": {"ollama": "qwen2.5-coder:7b"}},
        })

    def test_no_app_dir_returns_plain_fleet_routing(self):
        self.assertEqual(mr.load_routing_for_app(self.fleet, None),
                         mr.load_routing(self.fleet))
        self.assertEqual(mr.load_routing_for_app(self.fleet, ""),
                         mr.load_routing(self.fleet))

    def test_app_without_routing_file_falls_back_to_fleet(self):
        r = mr.load_routing_for_app(self.fleet, self.app)
        self.assertEqual(r, mr.load_routing(self.fleet))

    def test_app_field_overlays_fleet_field_per_phase(self):
        write_routing(self.app, {"phases": {
            "build_coordination": {"claude": "claude-opus-4-8"}}})
        r = mr.load_routing_for_app(self.fleet, self.app)
        # app's claude wins; fleet's codex_reasoning for the same phase survives
        self.assertEqual(r["phases"]["build_coordination"],
                         {"claude": "claude-opus-4-8", "codex_reasoning": "low"})
        # untouched fleet-only phase passes through unchanged
        self.assertEqual(r["phases"]["tech_specs"], {"ollama": "qwen2.5-coder:7b"})

    def test_app_enabled_and_fallback_never_shadow_fleet(self):
        # ModelRouting.save (GUI) always round-trips enabled=true and
        # cloud_to_local=true even though the Plan tab never edits them —
        # those must not clobber a deliberately different fleet setting.
        write_routing(self.app, {
            "enabled": True,
            "fallback": {"cloud_to_local": True, "local_model": "different:1b"},
            "phases": {"build_coordination": {"claude": "claude-opus-4-8"}},
        })
        r = mr.load_routing_for_app(self.fleet, self.app)
        self.assertFalse(r["fallback"]["cloud_to_local"])
        self.assertEqual(r["fallback"]["local_model"], "qwen2.5-coder:7b")

    def test_app_dir_same_as_fleet_dir_is_a_noop(self):
        self.assertEqual(mr.load_routing_for_app(self.fleet, self.fleet),
                         mr.load_routing(self.fleet))

    def test_empty_phases_with_default_fallback_does_not_warn(self):
        write_routing(self.app, {"enabled": True,
                                 "fallback": {"cloud_to_local": True, "local_model": ""}})
        warned = []
        mr.load_routing_for_app(self.fleet, self.app, on_warn=warned.append)
        self.assertEqual(warned, [])

    def test_empty_phases_with_real_override_warns(self):
        write_routing(self.app, {"enabled": False})
        warned = []
        r = mr.load_routing_for_app(self.fleet, self.app, on_warn=warned.append)
        self.assertEqual(r, mr.load_routing(self.fleet))
        self.assertEqual(len(warned), 1)
        self.assertIn("ignored", warned[0])


class TestOverridesAndFilter(unittest.TestCase):
    ROUTING = {"schema_version": 1, "enabled": True,
               "fallback": {"cloud_to_local": True, "local_model": ""},
               "phases": {"design_discussion": {"agents": "cloud",
                                                "claude": "claude-haiku-4-5"},
                          "build_coordination": {"agents": "claude, qwen3-coder:30b"},
                          "typo_phase": {"agents": "nonexistent-bot"}}}

    def test_overrides_disabled_routing_is_empty(self):
        off = dict(self.ROUTING, enabled=False)
        self.assertEqual(mr.overrides_for(off, "design_discussion"), {})
        self.assertEqual(mr.overrides_for(self.ROUTING, "unknown"), {})
        self.assertEqual(mr.overrides_for(self.ROUTING, "design_discussion")["claude"],
                         "claude-haiku-4-5")

    def test_filter_groups_and_ids(self):
        active = ["codex", "claude", "gemini", "local:qwen3-coder:30b", "ollama"]
        kept, note = mr.filter_agents(self.ROUTING, "design_discussion", active)
        self.assertEqual(kept, ["codex", "claude", "gemini"])
        self.assertIn("restricts participants", note)
        kept, _ = mr.filter_agents(self.ROUTING, "build_coordination", active)
        self.assertEqual(kept, ["claude", "local:qwen3-coder:30b"])

    def test_filter_fails_open(self):
        active = ["codex", "claude"]
        kept, note = mr.filter_agents(self.ROUTING, "typo_phase", active)
        self.assertEqual(kept, active)
        self.assertIn("ignoring", note)
        kept, note = mr.filter_agents(self.ROUTING, "no_such_phase", active)
        self.assertEqual((kept, note), (active, ""))


class TestFallbackModel(unittest.TestCase):
    def routing(self, explicit=""):
        return {"enabled": True,
                "fallback": {"cloud_to_local": True, "local_model": explicit}}

    def test_priority_order(self):
        installed = ["mistral:7b", "qwen2.5-coder:7b", "deepseek-r1:8b"]
        self.assertEqual(mr.fallback_model(self.routing("deepseek-r1:8b"),
                                           "qwen2.5-coder:7b", [], installed),
                         "deepseek-r1:8b")
        self.assertEqual(mr.fallback_model(self.routing(), "qwen2.5-coder:7b",
                                           [], installed), "qwen2.5-coder:7b")
        self.assertEqual(mr.fallback_model(self.routing(), "not-pulled:1b",
                                           ["also-missing:1b", "deepseek-r1:8b"],
                                           installed), "deepseek-r1:8b")
        # last resort: anything installed
        self.assertEqual(mr.fallback_model(self.routing(), "", [], installed),
                         "mistral:7b")
        self.assertEqual(mr.fallback_model(self.routing(), "x", ["y"], []), "")

    def test_fallback_enabled_gate(self):
        self.assertTrue(mr.fallback_enabled(self.routing()))
        self.assertFalse(mr.fallback_enabled({"enabled": False,
                                              "fallback": {"cloud_to_local": True}}))
        self.assertFalse(mr.fallback_enabled({"enabled": True,
                                              "fallback": {"cloud_to_local": False}}))


class TestApplyPhaseRouting(unittest.TestCase):
    def cfg(self):
        return {"models": {"claude": "claude-sonnet-5", "codex_reasoning": "low"},
                "_resolved": {"claude_model": "claude-sonnet-5",
                              "codex_model": "gpt-5.4", "ollama_model": ""},
                "_routing": {"enabled": True, "fallback": {},
                             "phases": {"build_coordination":
                                        {"claude": "claude-opus-4-8",
                                         "claude_reasoning": "high",
                                         "codex": "gpt-5.3-codex-spark",
                                         "codex_reasoning": "high"}}}}

    def test_overrides_patch_copy_not_original(self):
        cfg = self.cfg()
        c = orch._apply_phase_routing(cfg, "build_coordination")
        self.assertEqual(c["_resolved"]["claude_model"], "claude-opus-4-8")
        self.assertEqual(c["_resolved"]["codex_model"], "gpt-5.3-codex-spark")
        self.assertEqual(c["models"]["codex_reasoning"], "high")
        self.assertEqual(c["models"]["codex_build_reasoning"], "high")
        self.assertEqual(c["_phase_key"], "build_coordination")
        # original untouched
        self.assertEqual(cfg["_resolved"]["claude_model"], "claude-sonnet-5")
        self.assertNotIn("_phase_key", cfg)

    def test_claude_reasoning_override_sets_effort_pair(self):
        # Regression: PHASE_FIELDS used to omit "claude_reasoning", so
        # load_routing silently dropped it before it ever reached here.
        cfg = self.cfg()
        c = orch._apply_phase_routing(cfg, "build_coordination")
        self.assertEqual(c["models"]["claude_reasoning"], "high")
        self.assertEqual(c["models"]["claude_build_reasoning"], "high")

    def test_claude_reasoning_survives_load_routing_end_to_end(self):
        # The knob as it actually arrives: through mr.load_routing (validating
        # the raw JSON shape), not a hand-built _routing dict.
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {"phases": {"build_coordination":
                                         {"claude_reasoning": "high"}}})
            routing = mr.load_routing(d)
        cfg = self.cfg()
        cfg["_routing"] = routing
        c = orch._apply_phase_routing(cfg, "build_coordination")
        self.assertEqual(c["models"]["claude_reasoning"], "high")
        self.assertEqual(c["models"]["claude_build_reasoning"], "high")

    def test_uncached_routing_resolves_per_app_overlay(self):
        # No pre-seeded cfg["_routing"]: _apply_phase_routing must load it
        # itself via mrlib.load_routing_for_app(HERE, cfg["_app_dir"]), so a
        # project's model_routing.json actually reaches a real run.
        fleet = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, fleet, True)
        app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, app, True)
        write_routing(app, {"phases": {"build_coordination":
                                       {"claude_reasoning": "high"}}})
        old_here = orch.HERE
        orch.HERE = fleet
        try:
            cfg = {"models": {}, "_resolved": {}, "_app_dir": app}
            c = orch._apply_phase_routing(cfg, "build_coordination")
        finally:
            orch.HERE = old_here
        self.assertEqual(c["models"]["claude_reasoning"], "high")
        self.assertEqual(c["models"]["claude_build_reasoning"], "high")

    def test_unrouted_phase_gets_tagged_copy_sharing_state(self):
        cfg = self.cfg()
        c = orch._apply_phase_routing(cfg, "initial_discussion")
        self.assertEqual(c["_resolved"], cfg["_resolved"])
        self.assertEqual(c["_phase_key"], "initial_discussion")
        # cooldowns/sessions are SHARED dicts across phase copies. The health map
        # is _agent_health (the old "_health" key was written but never read).
        self.assertIs(c["_agent_health"], cfg["_agent_health"])
        self.assertIs(c["_claude_sessions"], cfg["_claude_sessions"])

    def test_enabled_agents_applies_phase_filter(self):
        cfg = {"agents": {"ollama_enabled": True},
               "models": {"ollama": "qwen2.5-coder:7b"}, "runtime": {},
               "_routing": {"enabled": True, "fallback": {},
                            "phases": {"design_discussion": {"agents": "cloud"}}},
               "_phase_key": "design_discussion"}
        self.assertEqual(orch.enabled_agents(cfg), ["codex", "claude", "gemini"])
        cfg["_phase_key"] = "other_phase"
        self.assertEqual(orch.enabled_agents(cfg),
                         ["codex", "claude", "gemini", "local:qwen2.5-coder:7b"])


class TestCallAgentFallback(unittest.TestCase):
    """A failed cloud turn is retried on an installed local model."""

    def setUp(self):
        self._once = orch._call_agent_once
        self._cached = lm.installed_models_cached
        lm.installed_models_cached = lambda ttl=60, run=None: ["qwen2.5-coder:7b"]

    def tearDown(self):
        orch._call_agent_once = self._once
        lm.installed_models_cached = self._cached

    def cfg(self, cloud_to_local=True):
        return {"models": {"ollama": "qwen2.5-coder:7b"}, "runtime": {},
                "_resolved": {"ollama_roster": []},
                "_routing": {"enabled": True,
                             "fallback": {"cloud_to_local": cloud_to_local,
                                          "local_model": ""}}}

    def test_cloud_failure_rescued_by_local(self):
        calls = []

        def fake_once(cfg, app, phase, rnd, agent, prompt):
            calls.append(agent)
            if agent == "codex":
                raise orch.AgentError("codex timed out")
            return "local answer"
        orch._call_agent_once = fake_once
        out = orch.call_agent(self.cfg(), "app", "tech_specs", 1, "codex", "P")
        self.assertEqual(calls, ["codex", "local:qwen2.5-coder:7b"])
        self.assertIn("Fallback:", out)
        self.assertIn("answered on this Mac", out)
        self.assertTrue(out.endswith("local answer"))

    def test_disabled_fallback_reraises(self):
        def fake_once(cfg, app, phase, rnd, agent, prompt):
            raise orch.AgentError("down")
        orch._call_agent_once = fake_once
        with self.assertRaises(orch.AgentError):
            orch.call_agent(self.cfg(cloud_to_local=False), "app", "p", 1, "codex", "P")

    def test_local_agent_never_falls_back(self):
        calls = []

        def fake_once(cfg, app, phase, rnd, agent, prompt):
            calls.append(agent)
            raise orch.AgentError("down")
        orch._call_agent_once = fake_once
        with self.assertRaises(orch.AgentError):
            orch.call_agent(self.cfg(), "app", "p", 1, "local:qwen2.5-coder:7b", "P")
        self.assertEqual(calls, ["local:qwen2.5-coder:7b"])

    def test_local_also_failing_raises_original_error(self):
        def fake_once(cfg, app, phase, rnd, agent, prompt):
            raise orch.AgentError("cloud broke" if agent == "claude" else "local broke")
        orch._call_agent_once = fake_once
        with self.assertRaises(orch.AgentError) as ctx:
            orch.call_agent(self.cfg(), "app", "p", 1, "claude", "P")
        self.assertIn("cloud broke", str(ctx.exception))


class TestFallbackChains(unittest.TestCase):
    def test_chains_parsed_and_junk_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {"fallback": {"chains": {
                "codex": ["gpt-5.3-codex-spark", "local:qwen3-coder:30b",
                          "bad\nvalue", 7],
                "gemini": ["gemini-3.5-flash"],
                "nonsense-agent": ["x"],
                "claude": "not a list",
            }}})
            r = mr.load_routing(d)
        self.assertEqual(mr.fallback_chain(r, "codex"),
                         ["gpt-5.3-codex-spark", "local:qwen3-coder:30b"])
        self.assertEqual(mr.fallback_chain(r, "gemini"), ["gemini-3.5-flash"])
        self.assertEqual(mr.fallback_chain(r, "claude"), [])
        self.assertNotIn("nonsense-agent", r["fallback"]["chains"])

    def test_phase_timeout_parsed_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            write_routing(d, {"phases": {
                "tech_specs": {"timeout": 1800},
                "bad": {"timeout": "junk"},
                "huge": {"timeout": 999999},
            }})
            r = mr.load_routing(d)
        self.assertEqual(mr.overrides_for(r, "tech_specs")["timeout"], 1800)
        self.assertNotIn("bad", r["phases"])
        self.assertNotIn("huge", r["phases"])

    def test_apply_phase_routing_sets_routed_timeout(self):
        cfg = {"models": {}, "_resolved": {},
               "_routing": {"enabled": True, "fallback": {},
                            "phases": {"tech_specs": {"timeout": 1800}}}}
        c = orch._apply_phase_routing(cfg, "tech_specs")
        self.assertEqual(c["_routed_turn_timeout"], 1800)

    def test_routed_timeout_exceeding_hard_timeout_is_logged(self):
        cfg = {"models": {}, "_resolved": {}, "runtime": {"timeout_seconds_per_agent": 600},
               "_routing": {"enabled": True, "fallback": {},
                            "phases": {"tech_specs": {"timeout": 1800}}}}
        msgs = []
        with unittest.mock.patch.object(orch, "emit", msgs.append):
            c = orch._apply_phase_routing(cfg, "tech_specs")
        self.assertEqual(c["_routed_turn_timeout"], 1800)
        self.assertTrue(any("exceeds" in m for m in msgs))

    def test_routed_timeout_within_hard_timeout_is_quiet(self):
        cfg = {"models": {}, "_resolved": {}, "runtime": {"timeout_seconds_per_agent": 3600},
               "_routing": {"enabled": True, "fallback": {},
                            "phases": {"tech_specs": {"timeout": 1800}}}}
        msgs = []
        with unittest.mock.patch.object(orch, "emit", msgs.append):
            orch._apply_phase_routing(cfg, "tech_specs")
        self.assertFalse(any("exceeds" in m for m in msgs))

    def test_ollama_override_shadowed_by_roster_is_logged(self):
        # A phase-level models.ollama override has NO effect on participants
        # when models.ollama_roster is configured (the roster wins in
        # enabled_agents()) — that silent no-op must be surfaced, not hidden.
        cfg = {"models": {"ollama_roster": "model-a, model-b"},
              "_resolved": {"ollama_roster": ["model-a", "model-b"]},
              "runtime": {}, "agents": {},
              "_routing": {"enabled": True, "fallback": {},
                           "phases": {"initial_discussion": {"ollama": "model-c"}}}}
        msgs = []
        with unittest.mock.patch.object(orch, "emit", msgs.append):
            orch._apply_phase_routing(cfg, "initial_discussion")
        self.assertTrue(any("has no effect on participants" in m for m in msgs))

    def test_ollama_override_not_shadowed_when_in_roster(self):
        # When the override DOES name a roster member, no confusing warning.
        cfg = {"models": {"ollama_roster": "model-a, model-b"},
              "_resolved": {"ollama_roster": ["model-a", "model-b"]},
              "runtime": {}, "agents": {},
              "_routing": {"enabled": True, "fallback": {},
                           "phases": {"initial_discussion": {"ollama": "model-a"}}}}
        msgs = []
        with unittest.mock.patch.object(orch, "emit", msgs.append):
            orch._apply_phase_routing(cfg, "initial_discussion")
        self.assertFalse(any("has no effect on participants" in m for m in msgs))

    def test_ollama_override_not_shadowed_when_no_roster(self):
        # With no roster at all, the legacy single-model override IS honored,
        # so no warning should fire.
        cfg = {"models": {}, "_resolved": {}, "runtime": {}, "agents": {},
              "_routing": {"enabled": True, "fallback": {},
                           "phases": {"initial_discussion": {"ollama": "model-c"}}}}
        msgs = []
        with unittest.mock.patch.object(orch, "emit", msgs.append):
            orch._apply_phase_routing(cfg, "initial_discussion")
        self.assertFalse(any("has no effect on participants" in m for m in msgs))


class TestCallAgentChain(unittest.TestCase):
    """The fallback ladder: sibling provider models first, then local."""

    def setUp(self):
        self._once = orch._call_agent_once
        self._cached = lm.installed_models_cached
        lm.installed_models_cached = lambda ttl=60, run=None: ["qwen2.5-coder:7b"]

    def tearDown(self):
        orch._call_agent_once = self._once
        lm.installed_models_cached = self._cached

    def cfg(self, chains):
        return {"models": {"ollama": ""}, "runtime": {},
                "_resolved": {"codex_model": "gpt-5.4", "claude_model": "claude-sonnet-5",
                              "ollama_roster": []},
                "_routing": {"enabled": True,
                             "fallback": {"cloud_to_local": True, "local_model": "",
                                          "chains": chains}}}

    def test_sibling_model_rescues_codex(self):
        seen = []

        def fake_once(cfg, app, phase, rnd, agent, prompt):
            model = (cfg.get("_resolved") or {}).get("codex_model")
            seen.append((agent, model))
            if model == "gpt-5.4":
                raise orch.AgentError("usage limit reached")
            return "spark answer"
        orch._call_agent_once = fake_once
        out = orch.call_agent(self.cfg({"codex": ["gpt-5.3-codex-spark"]}),
                              "app", "p", 1, "codex", "P")
        self.assertEqual(seen, [("codex", "gpt-5.4"), ("codex", "gpt-5.3-codex-spark")])
        self.assertIn("answered on gpt-5.3-codex-spark", out)
        self.assertTrue(out.endswith("spark answer"))

    def test_chain_walks_to_local_then_net(self):
        seen = []

        def fake_once(cfg, app, phase, rnd, agent, prompt):
            seen.append(agent if agent.startswith("local:")
                        else (cfg.get("_claude_model_override") or "primary"))
            if agent == "local:qwen2.5-coder:7b":
                return "local answer"
            raise orch.AgentError("down")
        orch._call_agent_once = fake_once
        c = self.cfg({"claude": ["claude-haiku-4-5", "local:not-pulled:1b"]})
        c["models"]["ollama"] = "qwen2.5-coder:7b"   # legacy net resolves to this
        out = orch.call_agent(c, "app", "p", 1, "claude", "P")
        # primary -> sibling retry -> (not-pulled skipped) -> legacy local net
        self.assertEqual(seen, ["primary", "claude-haiku-4-5", "local:qwen2.5-coder:7b"])
        self.assertIn("answered on this Mac", out)

    def test_all_steps_fail_raises_original(self):
        def fake_once(cfg, app, phase, rnd, agent, prompt):
            raise orch.AgentError("primary broke" if agent == "codex" else "step broke")
        orch._call_agent_once = fake_once
        with self.assertRaises(orch.AgentError) as ctx:
            orch.call_agent(self.cfg({"codex": ["gpt-5.3-codex-spark"]}),
                            "app", "p", 1, "codex", "P")
        self.assertIn("primary broke", str(ctx.exception))


class TestSearchRemote(unittest.TestCase):
    HF = [{"id": "unsloth/Qwen3-Coder-GGUF", "downloads": 51234, "likes": 900,
           "tags": ["gguf", "license:apache-2.0"]},
          {"id": "meta-llama/Gated-GGUF", "gated": True, "tags": ["gguf"]},
          {"id": "noslash", "tags": ["gguf"]}]

    def setUp(self):
        self._cached = lm.installed_models_cached
        lm.installed_models_cached = lambda ttl=60, run=None: ["qwen2.5-coder:7b"]

    def tearDown(self):
        lm.installed_models_cached = self._cached

    def test_merges_registry_and_hf_drops_gated(self):
        res = lm.search_remote("qwen", fetch=lambda url, timeout=10: self.HF, here=HERE)
        ids = [m["id"] for m in res["results"]]
        self.assertIn("qwen2.5-coder:7b", ids)                    # curated match
        self.assertIn("hf.co/unsloth/Qwen3-Coder-GGUF", ids)      # HF hit
        self.assertNotIn("hf.co/meta-llama/Gated-GGUF", ids)      # gated dropped
        hf = next(m for m in res["results"] if m["source"] == "huggingface")
        self.assertEqual(hf["license"], "apache-2.0")
        self.assertEqual(hf["downloads"], 51234)
        curated = next(m for m in res["results"] if m["id"] == "qwen2.5-coder:7b")
        self.assertTrue(curated["installed"])
        self.assertEqual(res["note"], "")

    def test_offline_degrades_to_curated_with_note(self):
        def boom(url, timeout=10):
            raise OSError("no network")
        res = lm.search_remote("qwen", fetch=boom, here=HERE)
        self.assertTrue(all(m["source"] == "curated" for m in res["results"]))
        self.assertIn("curated registry only", res["note"])

    def test_empty_query_lists_registry_without_network(self):
        def never(url, timeout=10):
            raise AssertionError("network must not be touched for empty query")
        res = lm.search_remote("", fetch=never, here=HERE, limit=50)
        self.assertEqual(len(res["results"]),
                         len(lm.load_registry(HERE)["models"]))

    def test_doctor_includes_model_routing_block(self):
        cfg = {"root": tempfile.gettempdir(), "models": {}, "_resolved": {}}
        rep = orch.preflight_report(cfg)
        self.assertIn("model_routing", rep)
        self.assertIn("fallback_cloud_to_local", rep["model_routing"])
        json.dumps(rep)   # stays JSON-serializable for --doctor --json


if __name__ == "__main__":
    unittest.main()
