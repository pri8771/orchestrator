import os, subprocess, tempfile, unittest
import orchestrator as orch


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git not available")
class TestGitBackedBuild(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_ensure_repo_idempotent(self):
        self.assertTrue(orch.ensure_build_repo(self.d))
        self.assertTrue(os.path.isdir(os.path.join(self.d, ".git")))
        self.assertTrue(orch.ensure_build_repo(self.d))  # second call no-op-safe

    def test_commit_only_on_change(self):
        orch.ensure_build_repo(self.d)
        self.assertEqual(orch.commit_build_state(self.d, "nothing"), "")  # no change
        with open(os.path.join(self.d, "A.swift"), "w") as fh:
            fh.write("struct A {}\n")
        sha = orch.commit_build_state(self.d, "add A")
        self.assertTrue(sha)
        # history now has commits
        code, out, _ = orch._git(self.d, "log", "--oneline")
        self.assertIn("add A", out)

    def test_tag_run(self):
        orch.ensure_build_repo(self.d)
        with open(os.path.join(self.d, "B.swift"), "w") as fh:
            fh.write("//\n")
        orch.commit_build_state(self.d, "b")
        orch.tag_build_run(self.d, "run-0001")
        code, out, _ = orch._git(self.d, "tag")
        self.assertIn("run-0001", out)

    def test_no_git_dir_is_safe(self):
        # commit on a non-repo dir returns '' rather than raising
        self.assertEqual(orch.commit_build_state(tempfile.mkdtemp(), "x"), "")


if __name__ == "__main__":
    unittest.main()
