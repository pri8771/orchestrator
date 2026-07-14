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

    def test_artifacts_and_secrets_not_committed(self):
        orch.ensure_build_repo(self.d)
        # Simulate agents deleting the .gitignore mid-build.
        os.remove(os.path.join(self.d, ".gitignore"))
        os.makedirs(os.path.join(self.d, "DerivedData", "x"))
        with open(os.path.join(self.d, "DerivedData", "x", "big.o"), "w") as fh:
            fh.write("junk")
        with open(os.path.join(self.d, "gemini_api_key"), "w") as fh:
            fh.write("SECRET")
        with open(os.path.join(self.d, "App.swift"), "w") as fh:
            fh.write("struct App {}\n")
        orch.commit_build_state(self.d, "iter")
        _, tracked, _ = orch._git(self.d, "ls-files")
        self.assertIn("App.swift", tracked)                 # real source committed
        self.assertNotIn("DerivedData", tracked)            # artifact ignored
        self.assertNotIn("gemini_api_key", tracked)         # secret ignored

    def test_gitignore_restored_when_deleted(self):
        orch.ensure_build_repo(self.d)
        os.remove(os.path.join(self.d, ".gitignore"))
        orch._ensure_build_gitignore(self.d)
        with open(os.path.join(self.d, ".gitignore")) as fh:
            body = fh.read()
        self.assertIn("DerivedData/", body)
        self.assertIn("*.key", body)


class TestParseMinYamlComments(unittest.TestCase):
    def test_inline_comment_stripped(self):
        cfg = orch.parse_min_yaml('models:\n  claude: "x"  # a note\n  n: 5 # count\n')
        self.assertEqual(cfg["models"]["claude"], "x")
        self.assertEqual(cfg["models"]["n"], 5)

    def test_hash_inside_quotes_kept(self):
        cfg = orch.parse_min_yaml('a:\n  tint: "#007AFF"\n')
        self.assertEqual(cfg["a"]["tint"], "#007AFF")

    def test_hash_without_leading_space_kept(self):
        cfg = orch.parse_min_yaml("a:\n  frag: http://x/y#z\n")
        self.assertEqual(cfg["a"]["frag"], "http://x/y#z")

    def test_value_that_is_only_a_comment_is_not_scalar(self):
        # `k: # comment` leaves an empty value, which this minimal parser treats
        # as an empty nested map (same as a bare `k:`) — the point is the comment
        # never becomes part of a scalar value.
        cfg = orch.parse_min_yaml("a:\n  k: # just a comment\n")
        self.assertEqual(cfg["a"]["k"], {})


if __name__ == "__main__":
    unittest.main()
