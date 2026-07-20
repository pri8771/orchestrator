"""V3 9.4 agent-library, presets, roster sizing, and lineage contracts."""
import json
import os
import shutil
import tempfile
import unittest

import artifacts
import modelrouting
import roles


class AgentLibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="agent-library-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "presets"))
        os.makedirs(os.path.join(self.root, "sections", "research", "presets"))

    def write(self, relative, obj):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def preset(self, relative, pid, temperature):
        self.write(relative, {"schema_version": 1, "id": pid,
                              "label": pid.title(),
                              "params": {"temperature": temperature}})

    def library(self, relative, personas, recommended=None):
        self.write(relative, {"schema_version": 1, "personas": personas,
                              "recommended_casts": recommended or {}})

    def test_section_shadows_fleet_persona_and_preset(self):
        self.preset("presets/tone.json", "tone", 1.1)
        self.preset("sections/research/presets/tone.json", "tone", 0.2)
        base = {"id": "investigator", "name": "Fleet", "preamble": "fleet",
                "backend": "api:openai", "model": "gpt", "preset": "tone"}
        local = dict(base, name="Section", preamble="section")
        self.library("agent_library.json", [base])
        self.library("sections/research/agent_library.json", [local])
        result = roles.load_agent_library(self.root, "research")
        self.assertEqual(result["personas_by_id"]["investigator"]["name"], "Section")
        self.assertEqual(result["presets"]["tone"]["params"]["temperature"], 0.2)
        self.assertEqual(result["personas_by_id"]["investigator"]["preset_params"],
                         {"temperature": 0.2})

    def test_library_persona_uses_existing_assignment_and_preamble_path(self):
        persona = {"id": "investigator", "name": "Investigator",
                   "preamble": "Follow evidence.", "backend": "codex",
                   "model": "gpt-5", "default_effort": "high"}
        self.library("agent_library.json", [persona])
        personalities, role_pool = roles.load_roles_layered(self.root)
        assigned = roles.assign_personas(0, ["codex"], personalities, role_pool,
                                         ["investigator"])["codex"]
        self.assertEqual(assigned["binding"],
                         {"backend": "codex", "model": "gpt-5",
                          "default_effort": "high"})
        self.assertIn("Investigator", roles.persona_preamble(assigned))
        self.assertIn("Follow evidence", roles.persona_preamble(assigned))

        ordinary = roles.assign_personas(0, ["codex"], personalities, role_pool)["codex"]
        self.assertNotIn("library_persona", ordinary,
                         "library entries are opt-in and never auto-join a cast")

    def test_cross_section_library_reference_and_dangling_warning(self):
        self.library("sections/research/agent_library.json", [{
            "id": "investigator", "name": "Investigator",
            "preamble": "Follow evidence.", "backend": "codex"}])
        warned = []
        kept, guests = roles.resolve_phase_role_refs(
            ["research:investigator", "research:missing"],
            os.path.join(self.root, "sections"),
            on_missing=lambda ref, why: warned.append((ref, why)))
        self.assertEqual(kept, ["research:investigator"])
        self.assertEqual(guests[0]["library_persona"]["id"], "investigator")
        self.assertEqual(warned[0][0], "research:missing")

    def test_corrupt_and_dangling_preset_warn_but_persona_loads(self):
        with open(os.path.join(self.root, "presets", "broken.json"), "w") as fh:
            fh.write("{bad")
        self.library("agent_library.json", [{
            "id": "safe", "name": "Safe", "preamble": "Continue.",
            "backend": "api:openai", "preset": "missing"}])
        warned = []
        result = roles.load_agent_library(self.root, on_warn=warned.append)
        self.assertIn("safe", result["personas_by_id"])
        self.assertNotIn("preset", result["personas_by_id"]["safe"])
        self.assertTrue(any("broken.json" in msg for msg in warned))
        self.assertTrue(any("missing preset" in msg for msg in warned))

    def test_incapable_binding_fields_are_visibly_ignored(self):
        self.preset("presets/tone.json", "tone", 0.3)
        self.library("agent_library.json", [{
            "id": "gem", "name": "Gem", "preamble": "Check.",
            "backend": "gemini", "model": "gemini-pro",
            "default_effort": "high", "preset": "tone"}])
        warned = []
        persona = roles.load_agent_library(
            self.root, on_warn=warned.append)["personas_by_id"]["gem"]
        self.assertNotIn("default_effort", persona)
        self.assertNotIn("preset", persona)
        self.assertTrue(any("no effort control" in msg for msg in warned))
        self.assertTrue(any("lacks sampling_params" in msg for msg in warned))

    def test_recommended_casts_are_data_only(self):
        recommended = {"ideation": {"count": 12, "note": "many"}}
        self.library("agent_library.json", [], recommended)
        before = modelrouting.filter_agents(
            {"enabled": True, "phases": {}}, "idea", ["claude", "codex"])
        os.remove(os.path.join(self.root, "agent_library.json"))
        after = modelrouting.filter_agents(
            {"enabled": True, "phases": {}}, "idea", ["claude", "codex"])
        self.assertEqual(before, after)


class RosterAndLineageTests(unittest.TestCase):
    def test_cast_size_truncates_repeats_and_empty_fails_open(self):
        active = ["claude", "codex", "gemini"]
        routing = {"enabled": True, "phases": {
            "small": {"cast_size": 2}, "large": {"cast_size": 5},
            "empty": {"cast_size": 0}}}
        self.assertEqual(modelrouting.filter_agents(routing, "small", active)[0],
                         ["claude", "codex"])
        self.assertEqual(modelrouting.filter_agents(routing, "large", active)[0],
                         ["claude", "codex", "gemini", "claude", "codex"])
        kept, note = modelrouting.filter_agents(routing, "empty", active)
        self.assertEqual(kept, active)
        self.assertIn("would empty", note)

    def test_composition_precedes_legacy_filter(self):
        routing = {"enabled": True, "phases": {"p": {
            "agents": "cloud", "composition": "claude,codex", "cast_size": 3}}}
        kept, _ = modelrouting.filter_agents(
            routing, "p", ["claude", "codex", "gemini", "ollama"])
        self.assertEqual(kept, ["claude", "codex", "claude"])

    def test_lineage_stamp_and_presetless_omission(self):
        root = tempfile.mkdtemp(prefix="agent-lineage-")
        self.addCleanup(shutil.rmtree, root, True)
        project, orch = os.path.join(root, "project"), os.path.join(root, "orch")
        os.makedirs(project)
        registry = artifacts.load_registry(orch)
        source = {"section": "research", "session": "s", "phase": "p", "turn": "t",
                  "persona_id": "investigator", "preset_id": "tone",
                  "preset_params": {"temperature": 0.3}}
        aid = artifacts.publish(project, "body", {"type": "idea", "title": "A",
                                                   "source": source}, registry,
                                consensus=True)
        meta = artifacts.load_meta(project, aid)
        self.assertEqual(meta["source"]["persona_id"], "investigator")
        self.assertEqual(meta["source"]["preset_id"], "tone")
        self.assertEqual(len(meta["source"]["preset_params_hash"]), 64)
        aid2 = artifacts.publish(project, "body2", {"type": "idea", "title": "B",
                                                     "source": {"section": "research"}},
                                 registry, consensus=True)
        source2 = artifacts.load_meta(project, aid2)["source"]
        self.assertNotIn("persona_id", source2)
        self.assertNotIn("preset_id", source2)


if __name__ == "__main__":
    unittest.main()
