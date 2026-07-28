"""End-to-end tests for run.sh's secret-refusal and git commit/push logic
(previously untested — flagged as an overflow item after the first audit
pass). Each test runs the REAL script as a subprocess against a scratch git
repo (ORCH_ROOT), so this exercises the actual bash logic, not a re-implementation
of it.
"""
import os
import re
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

    def _enable_commit_and_push(self):
        # commit_and_push now defaults to false (no auto-push without opt-in);
        # these tests exercise the commit/secret-refusal mechanics themselves,
        # so they opt back in explicitly via the same config.json override
        # mechanism run.sh actually reads.
        cfg_json = os.path.join(REPO_ROOT, "config.json")
        self.assertFalse(os.path.exists(cfg_json), "unexpected pre-existing config.json")
        with open(cfg_json, "w") as fh:
            fh.write('{"runtime": {"commit_and_push": true}}\n')
        self.addCleanup(lambda: os.path.exists(cfg_json) and os.remove(cfg_json))

    def test_secret_shaped_file_is_refused_not_committed(self):
        self._enable_commit_and_push()
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
        self._enable_commit_and_push()
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

    def test_gitignore_promised_secret_shapes_all_refused(self):
        # Regression (audit A-32): the guard previously missed *_api_key,
        # *.gemini_api_key and *.secret — names .gitignore / gui/build_app.sh
        # promise never to commit/ship — so a stray openai_api_key would have
        # been auto-committed and pushed. Every one must now be refused, while
        # an ordinary file in the same run still commits (no over-blocking).
        self._enable_commit_and_push()
        names = ["openai_api_key", "my_api_key", "foo.gemini_api_key",
                 "client.secret"]
        for name in names:
            with open(os.path.join(self.root, name), "w") as fh:
                fh.write("x\n")
        os.makedirs(os.path.join(self.root, "sub", "dir"))
        nested = os.path.join("sub", "dir", "openai_api_key")
        with open(os.path.join(self.root, nested), "w") as fh:
            fh.write("x\n")
        with open(os.path.join(self.root, "notes.md"), "w") as fh:
            fh.write("benign\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSING to commit secret-named files", proc.stdout)
        status = self._status()
        for name in names:
            self.assertIn(name, proc.stdout, "not refused: %s" % name)
            self.assertIn("?? %s" % name, status, "not unstaged: %s" % name)
        # The nested secret is refused by full path; git status collapses a
        # fully-untracked dir to "?? sub/", so assert on the tracked set.
        self.assertIn(nested, proc.stdout, "not refused: %s" % nested)
        tracked = subprocess.run(["git", "ls-files"], cwd=self.root,
                                 capture_output=True, text=True, check=True).stdout
        self.assertNotIn("openai_api_key", tracked)
        committed = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=self.root,
                                   capture_output=True, text=True).stdout
        for name in names:
            self.assertNotIn(name, committed, "committed anyway: %s" % name)
        self.assertIn("notes.md", committed,
                      "benign file must still be committed in the same run")

    def test_normal_files_are_committed_when_no_secrets_present(self):
        self._enable_commit_and_push()
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
        self._enable_commit_and_push()
        # A real repo reaches "nothing to commit" only after the workspace
        # .gitignore has already been seeded (orchestrator.py writes it once
        # per root, unconditionally) — pre-seed it here so this run doesn't
        # have that one genuinely-new file to commit.
        with open(os.path.join(self.root, ".gitignore"), "w") as fh:
            fh.write(".orchestrator_runtime/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed gitignore"],
                       cwd=self.root, check=True)
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

    def test_shipped_default_skips_git_step_with_no_config_override(self):
        # Regression: commit_and_push must default to false in the shipped
        # config.yaml — auto-pushing unreviewed, LLM-generated code to a real
        # remote shouldn't happen without an explicit opt-in.
        cfg_json = os.path.join(REPO_ROOT, "config.json")
        self.assertFalse(os.path.exists(cfg_json), "unexpected pre-existing config.json")
        with open(os.path.join(self.root, "notes.md"), "w") as fh:
            fh.write("hi\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("commit_and_push=false", proc.stdout)
        self.assertIn("skipping git", proc.stdout)
        self.assertNotIn("Committed", proc.stdout)


class TestRunShCredentialStripList(unittest.TestCase):
    """Static checks of run.sh's unset block (no git/bash subprocess needed)."""

    # Audit A-57: three drifting strip lists (run.sh=10 vars, RunController=8,
    # the enrollment launch=5) let a Vertex-credentialed environment bill
    # despite the README's no-cost promise. run.sh's unset block is the
    # canonical superset; the GUI mirrors it as APIKeyEnv.strippedAPIKeyVars
    # (RunController.swift), lockstep-checked on the Swift side by
    # APIKeyFileTests.testStrippedAPIKeyVarsMatchRunShUnsetBlock.
    CANONICAL = {
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_API_KEY",
    }

    def test_unset_block_strips_the_full_credential_superset(self):
        with open(RUN_SH) as fh:
            lines = fh.read().splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.startswith("unset ")),
            None)
        self.assertIsNotNone(start, "run.sh lost its unset block")
        joined = ""
        for line in lines[start:]:
            joined += " " + line.rstrip("\\")
            if not line.endswith("\\"):
                break
        # SHOUTING_CASE tokens only (drops `unset`, `2>/dev/null`, `|| true`).
        stripped = set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", joined))
        self.assertEqual(stripped, self.CANONICAL)


if __name__ == "__main__":
    unittest.main()
