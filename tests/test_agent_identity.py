import json, os, tempfile, unittest
import orchestrator as orch
import roles as roleslib


class TestAgentIdentity(unittest.TestCase):
    def test_dynamic_identity_not_dropped(self):
        active = ["codex", "claude", "local:qwen2.5-coder:7b"]
        ordered = orch.ordered_agents(active)
        self.assertIn("local:qwen2.5-coder:7b", ordered)
        # canonical trio keeps its order first, extras appended
        self.assertEqual(ordered[:2], ["codex", "claude"])

    def test_display_never_keyerrors(self):
        # would KeyError on a plain dict
        self.assertEqual(orch.DISPLAY["codex"], "Codex")
        label = orch.DISPLAY["local:qwen2.5-coder:7b"]
        self.assertIn("Qwen2.5", label)
        self.assertIn("local", label)

    def test_signature_derives(self):
        self.assertEqual(orch.SIGNATURE["claude"], "From Claude")
        self.assertTrue(orch.SIGNATURE["local:llama3.1:8b"].startswith("From "))

    def test_ordered_stable_and_deduped_source(self):
        self.assertEqual(orch.ordered_agents(["gemini", "codex"]), ["codex", "gemini"])
        # extras sorted deterministically
        self.assertEqual(orch.ordered_agents(["local:b", "local:a"]),
                         ["local:a", "local:b"])

    def test_agent_role_overrides_drive_personas(self):
        roles = [
            {"id": "product", "name": "Product", "focus": "scope"},
            {"id": "qa", "name": "QA", "focus": "risk"},
        ]
        personalities = [{"id": "skeptic", "name": "the Skeptic", "style": "push back"}]
        personas = roleslib.assign_personas(
            0, ["codex", "claude"], personalities, roles,
            phase_role_ids=["product"], agent_role_overrides={"codex": "qa"})
        self.assertEqual(personas["codex"]["role"]["id"], "qa")
        self.assertEqual(personas["claude"]["role"]["id"], "product")

    def test_load_agent_role_overrides(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "roles.json"), "w", encoding="utf-8") as fh:
            json.dump({"agent_role_overrides": {"codex": "frontend", "ollama": "qa"}}, fh)
        self.assertEqual(roleslib.load_agent_role_overrides(d),
                         {"codex": "frontend", "ollama": "qa"})

    def test_shipped_roles_json_has_no_pinned_overrides(self):
        # agent_role_overrides is a deliberate per-agent admin choice (GUI
        # "Configure -> Sub-agents" or hand-edited), not something that should
        # ship pre-pinned — a factory default here silently denies every fresh
        # project the role rotation roles.json's own comment promises.
        self.assertEqual(roleslib.load_agent_role_overrides(orch.HERE), {})


if __name__ == "__main__":
    unittest.main()
