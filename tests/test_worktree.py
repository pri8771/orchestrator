import os, subprocess, tempfile, unittest
import orchestrator as orch


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10); return True
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git not available")
class TestWorktreeIsolation(unittest.TestCase):
    def setUp(self):
        self.build = os.path.join(tempfile.mkdtemp(), "app_build")
        os.makedirs(self.build)
        orch.ensure_build_repo(self.build)
        self.roster = [{"slug": "codex", "agent": "codex", "label": "Codex", "lane": "core"},
                       {"slug": "claude", "agent": "claude", "label": "Claude", "lane": "ui"}]

    def test_setup_and_cleanup(self):
        wts = orch.setup_lane_worktrees(self.build, self.roster)
        self.assertEqual(set(wts), {"codex", "claude"})
        for p in wts.values():
            self.assertTrue(os.path.isdir(p))
        orch.cleanup_lane_worktrees(self.build, wts)

    def test_disjoint_lanes_merge_cleanly(self):
        wts = orch.setup_lane_worktrees(self.build, self.roster)
        # each lane writes a DIFFERENT file
        with open(os.path.join(wts["codex"], "Core.swift"), "w") as fh:
            fh.write("struct Core {}\n")
        with open(os.path.join(wts["claude"], "UI.swift"), "w") as fh:
            fh.write("struct UI {}\n")
        notes, blocked = orch.integrate_lane_worktrees(self.build, self.roster, wts)
        orch.cleanup_lane_worktrees(self.build, wts)
        self.assertEqual(notes, [])  # no conflict
        self.assertIsNone(blocked)
        # both files present in the integrated tree
        self.assertTrue(os.path.exists(os.path.join(self.build, "Core.swift")))
        self.assertTrue(os.path.exists(os.path.join(self.build, "UI.swift")))

    def test_conflicting_lanes_block_never_last_wins(self):
        """V2 spec §18.3 step 11 / §30: a real file conflict must produce
        blocked_conflict — NEVER a silent last-write-wins commit."""
        wts = orch.setup_lane_worktrees(self.build, self.roster)
        # both lanes write the SAME file differently -> conflict
        with open(os.path.join(wts["codex"], "Same.swift"), "w") as fh:
            fh.write("// codex version\n")
        with open(os.path.join(wts["claude"], "Same.swift"), "w") as fh:
            fh.write("// claude version\n")
        notes, blocked = orch.integrate_lane_worktrees(self.build, self.roster, wts)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["lane"], "claude")  # second lane hits the conflict
        self.assertIn("Same.swift", blocked["files"])
        self.assertTrue(any("blocked_conflict" in n for n in notes))
        # The integration branch must NOT contain a last-wins resolution commit.
        _code, log, _err = orch._git(self.build, "log", "--oneline")
        self.assertNotIn("last-lane-wins", log)
        # First lane's version merged cleanly and is intact (nothing overwrote it).
        with open(os.path.join(self.build, "Same.swift")) as fh:
            self.assertEqual(fh.read(), "// codex version\n")
        orch.cleanup_lane_worktrees(self.build, wts)

    def test_no_repo_returns_empty(self):
        self.assertEqual(orch.setup_lane_worktrees(tempfile.mkdtemp(), self.roster), {})


if __name__ == "__main__":
    unittest.main()
