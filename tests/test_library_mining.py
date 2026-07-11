import os, tempfile, unittest
import orchestrator as orch
import workflows as wf


class TestPortfolioTargets(unittest.TestCase):
    def setUp(self):
        self.app = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app, "initial_prompt"))
        # two fake repos with predictable names
        self.repos = []
        root = tempfile.mkdtemp()
        for name in ("repoA", "repoB"):
            r = os.path.join(root, name)
            os.makedirs(r)
            with open(os.path.join(r, "Net.swift"), "w") as f:
                f.write("struct Net {}\n")
            self.repos.append(r)

    def test_read_target_paths_multi(self):
        with open(os.path.join(self.app, "target_path.txt"), "w") as f:
            f.write("\n".join(self.repos) + "\n")
        paths = wf.read_target_paths(self.app)
        self.assertEqual(sorted(paths), sorted(os.path.realpath(r) for r in self.repos))

    def test_read_target_paths_dedups_and_guards(self):
        with open(os.path.join(self.app, "target_path.txt"), "w") as f:
            f.write(self.repos[0] + "\n" + self.repos[0] + "\n/nonexistent/xyz\n# comment\n")
        paths = wf.read_target_paths(self.app)
        self.assertEqual(len(paths), 1)  # dedup + drop nonexistent + skip comment

    def test_portfolio_digest_covers_all_repos(self):
        digest = orch.build_portfolio_digest(self.repos)
        self.assertIn("PORTFOLIO", digest)
        self.assertIn("repoA", digest)
        self.assertIn("repoB", digest)
        self.assertIn("Net.swift", digest)

    def test_empty_portfolio(self):
        self.assertEqual(orch.build_portfolio_digest([]), "")

    def test_library_mining_workflow_loads(self):
        w = wf.load_workflow("library_mining")
        self.assertEqual(w.target, "library_mining")
        self.assertEqual([p.key for p in w.phases],
                         ["portfolio_recon", "commonality_analysis", "extraction_candidates"])
        self.assertTrue(all(p.reads_target for p in w.phases))


if __name__ == "__main__":
    unittest.main()
