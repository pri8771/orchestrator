"""Tests for roles.py — deterministic (role, personality) assignment.

The README promises the pairing is deterministic (a resumed run reproduces it)
and that personalities rotate so no agent is locked to one temperament.
"""
import tempfile
import unittest

import roles


class TestAssignPersonas(unittest.TestCase):
    def setUp(self):
        self.personalities, self.roles = roles.load_roles()
        self.agents = ["codex", "claude", "gemini"]

    def _assign(self, phase_index, agents=None):
        return roles.assign_personas(phase_index, agents or self.agents,
                                     self.personalities, self.roles)

    def test_deterministic(self):
        a = self._assign(3)
        b = self._assign(3)
        self.assertEqual({k: (v["role"]["id"], v["personality"]["id"]) for k, v in a.items()},
                         {k: (v["role"]["id"], v["personality"]["id"]) for k, v in b.items()})

    def test_distinct_personalities_within_a_phase(self):
        a = self._assign(0)
        pers = [v["personality"]["id"] for v in a.values()]
        self.assertEqual(len(pers), len(set(pers)),
                         "agents in one phase must not share a personality")

    def test_personality_rotates_across_phases(self):
        # A given agent's personality changes as the phase index advances.
        seen = set()
        for phase in range(len(self.personalities)):
            seen.add(self._assign(phase)["codex"]["personality"]["id"])
        self.assertGreater(len(seen), 1, "codex was locked to one personality")

    def test_every_agent_gets_role_and_personality(self):
        a = self._assign(2)
        self.assertEqual(set(a), set(self.agents))
        for v in a.values():
            self.assertIn("id", v["role"])
            self.assertIn("id", v["personality"])

    def test_role_override_respected(self):
        override_role = self.roles[-1]["id"]
        a = roles.assign_personas(0, self.agents, self.personalities, self.roles,
                                  agent_role_overrides={"claude": override_role})
        self.assertEqual(a["claude"]["role"]["id"], override_role)

    def test_override_wins_even_outside_phase_role_restriction(self):
        # agent_role_overrides is a deliberate per-agent admin choice
        # (roles.json / GUI "Configure -> Sub-agents") and is meant to win over
        # a phase's generic role restriction — see
        # test_agent_role_overrides_drive_personas in test_agent_identity.py,
        # which pins this as intentional, not a bug.
        subset = [self.roles[0]["id"], self.roles[1]["id"]]
        excluded_role = self.roles[2]["id"]
        a = roles.assign_personas(0, self.agents, self.personalities, self.roles,
                                  phase_role_ids=subset,
                                  agent_role_overrides={"claude": excluded_role})
        self.assertEqual(a["claude"]["role"]["id"], excluded_role)

    def test_phase_role_subset_restricts_pool(self):
        subset = [self.roles[0]["id"], self.roles[1]["id"]]
        a = roles.assign_personas(0, self.agents, self.personalities, self.roles,
                                  phase_role_ids=subset)
        for v in a.values():
            self.assertIn(v["role"]["id"], subset)

    def test_load_roles_returns_independent_copies_on_fallback(self):
        # With no roles.json present, load_roles falls back to the built-in
        # DEFAULT_PERSONALITIES/DEFAULT_ROLES. Each call must return an
        # independent copy — a caller mutating its result must not corrupt the
        # module-level defaults (and therefore every other concurrent caller).
        with tempfile.TemporaryDirectory() as d:
            personalities_a, roles_a = roles.load_roles(d)
            personalities_a.append({"id": "poisoned"})
            roles_a.append({"id": "poisoned"})
            personalities_b, roles_b = roles.load_roles(d)
        self.assertNotIn("poisoned", [p.get("id") for p in personalities_b])
        self.assertNotIn("poisoned", [r.get("id") for r in roles_b])
        self.assertNotIn("poisoned", [p.get("id") for p in roles.DEFAULT_PERSONALITIES])
        self.assertNotIn("poisoned", [r.get("id") for r in roles.DEFAULT_ROLES])

    def test_load_roles_mutating_returned_dicts_does_not_leak(self):
        with tempfile.TemporaryDirectory() as d:
            personalities_a, _ = roles.load_roles(d)
            personalities_a[0]["name"] = "POISONED"
            personalities_b, _ = roles.load_roles(d)
        self.assertNotEqual(personalities_b[0]["name"], "POISONED")

    def test_empty_pools_do_not_raise(self):
        # A caller passing gutted pools must not hit ZeroDivisionError; falls
        # back to the built-in defaults so every agent still gets a persona.
        a = roles.assign_personas(0, self.agents, [], [])
        self.assertEqual(set(a), set(self.agents))
        for v in a.values():
            self.assertIn("id", v["role"])
            self.assertIn("id", v["personality"])

    def test_persona_label_is_short_tag(self):
        a = self._assign(0)
        label = roles.persona_label(a["codex"])
        self.assertTrue(label)
        self.assertIn("·", label)


if __name__ == "__main__":
    unittest.main()
