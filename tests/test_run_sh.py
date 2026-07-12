"""End-to-end tests for run.sh's secret-refusal and git commit/push logic
(previously untested — flagged as an overflow item after the first audit
pass). Each test runs the REAL script as a subprocess against a scratch git
repo (ORCH_ROOT), so this exercises the actual bash logic, not a re-implementation
of it.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
RUN_SH = os.path.join(REPO_ROOT, "run.sh")


def _has_git_and_bash():
    return shutil.which("git") is not None and shutil.which("bash") is not None


@unittest.skipUnless(_has_git_and_bash(), "requires git and bash")
class TestRunShGitBehavior(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_runsh_")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                       cwd=self.root, check=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, *args, extra_env=None):
        env = dict(os.environ)
        env["ORCH_ROOT"] = self.root
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", RUN_SH, *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60, env=env)

    def _log(self):
        out = subprocess.run(["git", "log", "--oneline"], cwd=self.root,
                             capture_output=True, text=True, check=True)
        return out.stdout

    def _status(self):
        out = subprocess.run(["git", "status", "--short"], cwd=self.root,
                             capture_output=True, text=True, check=True)
        return out.stdout

    def test_secret_shaped_file_is_refused_not_committed(self):
        with open(os.path.join(self.root, ".env"), "w") as fh:
            fh.write("SECRET_KEY=abc123\n")
        with open(os.path.join(self.root, "normal.md"), "w") as fh:
            fh.write("hello\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSING to commit secret-named files", proc.stdout)
        self.assertIn(".env", proc.stdout)
        # The secret file is unstaged (still present, untracked) — not committed.
        log = self._log()
        self.assertNotIn(".env", subprocess.run(
            ["git", "show", "--stat", "HEAD"], cwd=self.root,
            capture_output=True, text=True).stdout)
        self.assertIn("?? .env", self._status())
        self.assertIn("normal.md", log if False else
                      subprocess.run(["git", "show", "--stat", "HEAD"], cwd=self.root,
                                     capture_output=True, text=True).stdout)

    def test_various_secret_shapes_all_refused(self):
        names = ["gemini_api_key", "config.json", ".env.local",
                "creds.pem", "id.key", "cert.p12"]
        for name in names:
            with open(os.path.join(self.root, name), "w") as fh:
                fh.write("x\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in names:
            self.assertIn(name, proc.stdout, "not refused: %s" % name)
            self.assertIn("?? %s" % name, self._status(), "not unstaged: %s" % name)

    def test_normal_files_are_committed_when_no_secrets_present(self):
        with open(os.path.join(self.root, "notes.md"), "w") as fh:
            fh.write("just notes\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Committed", proc.stdout)
        self.assertNotIn("REFUSING", proc.stdout)
        self.assertIn("notes.md", subprocess.run(
            ["git", "show", "--stat", "HEAD"], cwd=self.root,
            capture_output=True, text=True).stdout)

    def test_nothing_to_commit_is_reported_cleanly(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Nothing to commit", proc.stdout)

    def test_doctor_flag_skips_git_entirely(self):
        with open(os.path.join(self.root, ".env"), "w") as fh:
            fh.write("SECRET=1\n")
        proc = self._run("--doctor")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("doctor run — skipping git", proc.stdout)
        # git add -A never even ran — the secret file is untouched/untracked,
        # no REFUSING message (nothing was staged to refuse).
        self.assertNotIn("REFUSING", proc.stdout)

    def test_commit_and_push_false_skips_git_step(self):
        cfg_json = os.path.join(REPO_ROOT, "config.json")
        # config.json is itself secret-shaped and gitignored — write it only for
        # the duration of this test, directly in the engine dir (matches how
        # run.sh actually reads CP_SRC), then remove it.
        self.assertFalse(os.path.exists(cfg_json), "unexpected pre-existing config.json")
        try:
            with open(cfg_json, "w") as fh:
                fh.write('{"runtime": {"commit_and_push": false}}\n')
            with open(os.path.join(self.root, "notes.md"), "w") as fh:
                fh.write("hi\n")
            proc = self._run()
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("commit_and_push=false", proc.stdout)
            self.assertIn("skipping git", proc.stdout)
            self.assertNotIn("Committed", proc.stdout)
        finally:
            if os.path.exists(cfg_json):
                os.remove(cfg_json)


if __name__ == "__main__":
    unittest.main()
