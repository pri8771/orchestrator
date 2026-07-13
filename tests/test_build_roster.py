"""build_worker_roster must give every build lane an owner.

The planner is told all four BUILD_LANE_IDS are valid owner_lane values, so a
roster with fewer workers than lanes leaves a lane (e.g. polish_resilience)
owned by no worker — its tasks.json items are then shown to nobody and never
built. These tests pin lane coverage across CLI-count scenarios.
"""
import unittest
import unittest.mock

import orchestrator as orch


class TestRosterLaneCoverage(unittest.TestCase):
    def _roster(self, available):
        cfg = {"runtime": {"build_parallel_workers": 3}}
        with unittest.mock.patch.object(
                orch, "_agent_available", side_effect=lambda a, cfg=None: a in available):
            return orch.build_worker_roster(cfg, list(available) or ["codex"])

    def _lanes(self, roster):
        return {w["lane_id"] for w in roster}

    def test_three_distinct_clis_still_covers_all_lanes(self):
        roster = self._roster(["codex", "claude", "gemini"])
        self.assertEqual(self._lanes(roster), set(orch.BUILD_LANE_IDS))
        self.assertGreaterEqual(len(roster), len(orch.BUILD_LANE_IDS))

    def test_two_distinct_clis_covers_all_lanes(self):
        roster = self._roster(["codex", "claude"])
        self.assertEqual(self._lanes(roster), set(orch.BUILD_LANE_IDS))

    def test_single_cli_covers_all_lanes(self):
        roster = self._roster(["codex"])
        self.assertEqual(self._lanes(roster), set(orch.BUILD_LANE_IDS))
        # A replicated CLI must get distinct slugs so worktrees don't collide.
        self.assertEqual(len({w["slug"] for w in roster}), len(roster))

    def test_replicated_workers_have_unique_labels_and_slugs(self):
        roster = self._roster(["codex", "claude", "gemini"])
        self.assertEqual(len({w["slug"] for w in roster}), len(roster))

    def test_every_worker_has_a_valid_lane(self):
        roster = self._roster(["codex", "claude", "gemini"])
        for w in roster:
            self.assertIn(w["lane_id"], orch.BUILD_LANE_IDS)
            self.assertTrue(w["lane"])


if __name__ == "__main__":
    unittest.main()
