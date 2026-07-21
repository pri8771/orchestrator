"""V3 8.5: privacy is enforced at execution, context, and routing seams."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import artifacts
import conductor
import events
import localmodels
import orchestrator as orch


class PrivacyFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="privacy-8.5-")
        self.project_dir = os.path.join(self.root, "demo")
        self.sid = "demo/ideas/source"
        self.app_dir = os.path.join(self.root, self.sid)
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        open(os.path.join(self.project_dir, ".orch-sections"), "a").close()
        with open(os.path.join(self.app_dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("privacy test\n")
        self.registry = artifacts.load_registry(orch.HERE)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def private_cfg(self):
        state = orch.load_state(self.app_dir)
        state["sensitivity"] = "private"
        return {"root": self.root, "_app_dir": self.app_dir,
                "_state": state, "_sensitivity": "private",
                "_agent_health": {}, "runtime": {}, "models": {},
                "_resolved": {}}

    def publish(self, title, sensitivity, body="privacy caching evidence"):
        return artifacts.publish(
            self.project_dir, body,
            {"type": "idea", "title": title, "sensitivity": sensitivity,
             "source": {"section": "ideas", "session": "source",
                        "phase": "research", "turn": "r1"}},
            self.registry, consensus=True)


class TestRunnerBoundary(PrivacyFixture):
    def test_private_turn_never_invokes_installed_cloud_callable(self):
        calls = []

        def cloud(_cfg, prompt, _timeout):
            calls.append(prompt)
            return ("cloud output", "", 0, ["cloud"])

        cfg = self.private_cfg()
        with mock.patch.dict(orch.RUNNERS, {"codex": cloud}):
            with self.assertRaises(orch.PrivacyViolation) as caught:
                orch.call_agent(cfg, "demo", "design", 1, "codex",
                                "PRIVATE-CONTENT-MUST-NOT-LEAVE")
        self.assertEqual(calls, [])
        self.assertIn("Ollama", str(caught.exception))
        records = events.read_events(self.app_dir)
        self.assertTrue(any(r.get("kind") == "privacy_blocked"
                            and r.get("agent") == "codex" for r in records))
        self.assertEqual(len([r for r in records
                              if r.get("kind") == "turn_started"]), 1)
        completed = [r for r in records
                     if r.get("kind") == "turn_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["reason"], "privacy_blocked")

    def test_private_api_refusal_precedes_stream_or_network_setup(self):
        cfg = self.private_cfg()
        cfg["_api_agents_enabled"] = True
        with self.assertRaises(orch.PrivacyViolation):
            orch.call_agent(cfg, "demo", "design", 1,
                            "api:openai:gpt-private", "SENSITIVE")
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, ".stream")))

    def test_private_fallback_chain_is_local_only_and_visible(self):
        cfg = self.private_cfg()
        cfg["_routing"] = {
            "enabled": True,
            "fallback": {"cloud_to_local": True,
                         "chains": {"codex": ["gpt-cloud",
                                                "local:tiny:1b"]}}}
        steps = orch._fallback_steps(cfg, "codex")
        self.assertIn("local:tiny:1b", steps)
        self.assertTrue(steps)
        self.assertTrue(all(step.startswith("local:") for step in steps))
        self.assertNotIn("gpt-cloud", steps)
        records = events.read_events(self.app_dir)
        enforced = [r for r in records
                    if r.get("kind") == "privacy_enforced"]
        self.assertEqual(len(enforced), 1)
        self.assertIn("gpt-cloud", enforced[0]["reason"])

    def test_roster_rewrites_to_installed_local_or_refuses_before_turn(self):
        cfg = self.private_cfg()
        cfg.update({"agents": {"codex_enabled": True,
                               "claude_enabled": True,
                               "gemini_enabled": False,
                               "ollama_enabled": True},
                    "models": {"ollama": "tiny:1b"},
                    "runtime": {"enforce_local_ram_gate": False},
                    "_resolved": {"ollama_roster": ["tiny:1b"]}})
        with mock.patch.object(localmodels, "installed_models_cached",
                               return_value=["tiny:1b"]):
            self.assertEqual(orch.enabled_agents(cfg), ["local:tiny:1b"])
        cfg["_installed_ollama_models"] = []
        cfg.pop("_privacy_notes", None)
        with self.assertRaises(orch.PrivacyViolation):
            orch.enabled_agents(cfg)


class TestArtifactPrivacy(PrivacyFixture):
    def test_schema_lineage_and_context_matrix_preserve_private(self):
        normal = self.publish("Normal caching", "normal")
        private = self.publish("Private caching", "private")
        bad = self.publish("Bad", "secret")
        self.assertIsNone(bad)
        child = artifacts.publish(
            self.project_dir, "new private caching evidence",
            {"type": "idea", "title": "Private caching v2",
             "sensitivity": "normal",
             "source": {"section": "research", "session": "source",
                        "phase": "research", "turn": "r2"}},
            self.registry, supersedes=private, consensus=True)
        self.assertEqual(artifacts.load_meta(
            self.project_dir, child)["sensitivity"], "private")

        normal_cfg = {"_sensitivity": "normal", "_state": {}}
        private_cfg = {"_sensitivity": "private", "_state": {}}
        normal_context = artifacts.retrieve(
            self.project_dir, "caching evidence", top_k=10,
            sensitivity_filter=orch._artifact_sensitivity_filter(normal_cfg))
        private_context = artifacts.retrieve(
            self.project_dir, "caching evidence", top_k=10,
            sensitivity_filter=orch._artifact_sensitivity_filter(private_cfg))
        self.assertIn(normal, normal_context)
        self.assertNotIn(child, normal_context)
        self.assertIn(normal, private_context)
        self.assertIn(child, private_context)

    def test_resume_rederives_private_from_config_then_persisted_state(self):
        with open(os.path.join(self.project_dir, "run_config.json"), "w") as fh:
            json.dump({"sensitivity": "private"}, fh)
        self.assertEqual(orch._effective_sensitivity(
            self.app_dir, self.root, {}), "private")
        os.remove(os.path.join(self.project_dir, "run_config.json"))
        self.assertEqual(orch._effective_sensitivity(
            self.app_dir, self.root, {"sensitivity": "private"}), "private")
        with open(os.path.join(self.project_dir, "run_config.json"), "w") as fh:
            json.dump({"sensitivity": "normal"}, fh)
        self.assertEqual(orch._effective_sensitivity(
            self.app_dir, self.root, {"sensitivity": "private"}), "normal")
        self.assertEqual(artifacts.load_meta(
            self.project_dir, self.publish("Stays private", "private"))[
                "sensitivity"], "private")


class TestPrivateRouting(PrivacyFixture):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.project_dir, "routing.json"), "w") as fh:
            json.dump({"rules": [{
                "rule_id": "private-to-research",
                "match": {"artifact_type": "idea"}, "strategy": "one",
                "targets": ["research"], "hop_budget": 2}]}, fh)

    def route(self, local_available):
        state = conductor.default_state()
        state["oversight"] = {"dial": "full_auto"}
        with mock.patch.object(orch, "privacy_target_has_local_runner",
                               return_value=local_available):
            return conductor.route_engine(
                self.root, state, [self.sid], emit=lambda _m: None)

    def test_private_route_blocks_without_local_and_never_mints(self):
        aid = self.publish("Private route", "private")
        state = self.route(False)
        ledger = conductor.read_ledger(self.root)
        blocked = [r for r in ledger
                   if r and r.get("decision") == "privacy_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["detail"]["artifact_id"], aid)
        self.assertEqual(len(state["routed"]), 1)
        target = os.path.join(self.project_dir, "research")
        self.assertFalse(os.path.isdir(target))
        self.assertTrue(any(r.get("kind") == "privacy_blocked"
                            for r in events.read_events(self.app_dir)))

    def test_private_route_stamps_child_before_it_can_run(self):
        aid = self.publish("Private routed", "private")
        self.route(True)
        target = os.path.join(self.project_dir, "research")
        children = [os.path.join(target, name) for name in os.listdir(target)]
        self.assertEqual(len(children), 1)
        with open(os.path.join(children[0], "run_config.json")) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["sensitivity"], "private")
        with open(os.path.join(children[0], "delegation.json")) as fh:
            delegation = json.load(fh)
        self.assertEqual(delegation["request"]["artifact_id"], aid)
        self.assertEqual(delegation["request"]["sensitivity"], "private")
        os.remove(os.path.join(children[0], "run_config.json"))
        self.assertEqual(orch._effective_sensitivity(
            children[0], self.root, {}), "private",
            "delegation evidence closes the mint-to-config crash gap")


if __name__ == "__main__":
    unittest.main()
