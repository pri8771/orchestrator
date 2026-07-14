"""Tests for shepherd.sh's pure-logic pieces (previously untested — flagged as
an overflow item after the first audit pass): queue_order_dirs, retry_ok, and
the auto-repair-queue Python heredoc. shepherd.sh itself is an infinite
`while true; do ... sleep 120; done` loop that launches real builds, so it
can't be run end-to-end in a test; instead we source everything BEFORE the
`while true` line (all function definitions) into a fresh bash and call the
functions directly, and extract+run the embedded Python heredoc block
in isolation against fixture app directories.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SHEPHERD_SH = os.path.join(REPO_ROOT, "shepherd.sh")


def _has_git_and_bash():
    return shutil.which("bash") is not None


def _functions_only_source():
    """Everything up to (not including) the `while true; do` main loop."""
    with open(SHEPHERD_SH) as fh:
        text = fh.read()
    idx = text.index("\nwhile true; do")
    return text[:idx]


def _heredoc_python_source():
    """Extract the body of the `python3 - "$ROOT" <<'PY' ... PY` block."""
    with open(SHEPHERD_SH) as fh:
        text = fh.read()
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.DOTALL)
    assert m, "could not locate the auto-repair PY heredoc in shepherd.sh"
    return m.group(1)


@unittest.skipUnless(_has_git_and_bash(), "requires bash")
class TestShepherdFunctions(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_shepherd_")
        self.funcs = _functions_only_source()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, snippet):
        script = "%s\nROOT=%r\n%s\n" % (self.funcs, self.root, snippet)
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=30)

    def test_queue_order_dirs_lists_only_string_entries_without_slash(self):
        with open(os.path.join(self.root, ".orch-queue-order.json"), "w") as fh:
            json.dump({"order": ["AppOne", "AppTwo", "bad/slash", 42, ""]}, fh)
        proc = self._run("queue_order_dirs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l]
        self.assertIn(os.path.join(self.root, "AppOne") + "/", lines)
        self.assertIn(os.path.join(self.root, "AppTwo") + "/", lines)
        self.assertFalse(any("bad/slash" in l for l in lines))

    def test_queue_order_dirs_empty_without_queue_file(self):
        # No .orch-queue-order.json present: python3 fails to open it (exit
        # non-zero, same as shepherd.sh sees on a fresh ROOT), but stdout
        # must still be empty — nothing gets queued.
        proc = self._run("queue_order_dirs")
        self.assertEqual(proc.stdout.strip(), "")

    def test_queue_lanes_reads_positive_int(self):
        with open(os.path.join(self.root, ".orch-queue-order.json"), "w") as fh:
            json.dump({"lanes": 5}, fh)
        proc = self._run("queue_lanes")
        self.assertEqual(proc.stdout.strip(), "5")

    def test_queue_lanes_ignores_non_positive_or_missing(self):
        with open(os.path.join(self.root, ".orch-queue-order.json"), "w") as fh:
            json.dump({"lanes": 0}, fh)
        proc = self._run("queue_lanes")
        self.assertEqual(proc.stdout.strip(), "")

    def test_retry_ok_allows_three_then_refuses(self):
        os.makedirs(os.path.join(self.root, "app1"))
        for i in range(3):
            proc = self._run('retry_ok app1; echo "rc=$?"')
            self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)
        proc = self._run('retry_ok app1; echo "rc=$?"')
        self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
        with open(os.path.join(self.root, "app1", ".shepherd_retries")) as fh:
            self.assertEqual(fh.read().strip(), "3")

    def test_is_done_true_and_false(self):
        os.makedirs(os.path.join(self.root, "app1"))
        with open(os.path.join(self.root, "app1", "agent_state.json"), "w") as fh:
            json.dump({"done": True}, fh)
        proc = self._run('is_done app1; echo "rc=$?"')
        self.assertIn("rc=0", proc.stdout)

        os.makedirs(os.path.join(self.root, "app2"))
        with open(os.path.join(self.root, "app2", "agent_state.json"), "w") as fh:
            json.dump({"done": False}, fh)
        proc = self._run('is_done app2; echo "rc=$?"')
        self.assertIn("rc=1", proc.stdout)

    def test_has_error_true_only_when_error_and_not_done(self):
        os.makedirs(os.path.join(self.root, "app1"))
        with open(os.path.join(self.root, "app1", "agent_state.json"), "w") as fh:
            json.dump({"error": "boom", "done": False}, fh)
        proc = self._run('has_error app1; echo "rc=$?"')
        self.assertIn("rc=0", proc.stdout)

        os.makedirs(os.path.join(self.root, "app2"))
        with open(os.path.join(self.root, "app2", "agent_state.json"), "w") as fh:
            json.dump({"error": "boom", "done": True}, fh)
        proc = self._run('has_error app2; echo "rc=$?"')
        self.assertIn("rc=1", proc.stdout)

    def test_is_parent_matches_whole_names_only(self):
        proc = self._run('PARENTS=(Foo Bar); is_parent Foo; echo "rc=$?"')
        self.assertIn("rc=0", proc.stdout)
        proc = self._run('PARENTS=(Foo Bar); is_parent FooBaz; echo "rc=$?"')
        self.assertIn("rc=1", proc.stdout)


@unittest.skipUnless(shutil.which("python3"), "requires python3")
class TestShepherdAutoRepairHeredoc(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_shepherd_repair_")
        self.py_src = _heredoc_python_source()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_app(self, name, done=True, verify_ok=False, verify_summary=""):
        d = os.path.join(self.root, name)
        os.makedirs(os.path.join(d, "initial_prompt"))
        with open(os.path.join(d, "agent_state.json"), "w") as fh:
            json.dump({"done": done}, fh)
        with open(os.path.join(d, "initial_prompt", "initial_prompt.md"), "w") as fh:
            fh.write("# Build me an app\n")
        with open(os.path.join(d, "verify_results.json"), "w") as fh:
            json.dump([{"ok": verify_ok, "summary": verify_summary}], fh)
        return d

    def _run_heredoc(self):
        return subprocess.run(["python3", "-c", self.py_src, self.root],
                              capture_output=True, text=True, timeout=30)

    def test_queues_repair_for_done_app_with_failed_verify(self):
        d = self._make_app("appA", done=True, verify_ok=False,
                           verify_summary="build failed: syntax error")
        proc = self._run_heredoc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("appA", proc.stdout)
        self.assertTrue(os.path.exists(os.path.join(d, ".repair_pending")))
        self.assertTrue(os.path.exists(os.path.join(d, ".repair_attempted")))
        with open(os.path.join(d, "workflow.txt")) as fh:
            self.assertEqual(fh.read().strip(), "iterate")
        with open(os.path.join(d, "initial_prompt", "initial_prompt.md")) as fh:
            text = fh.read()
        self.assertIn("Change requested", text)
        self.assertIn("FAILS to compile", text)

    def test_uses_no_xcodeproj_wording_when_summary_matches(self):
        d = self._make_app("appB", done=True, verify_ok=False,
                           verify_summary="no .xcodeproj found in the repo")
        self._run_heredoc()
        with open(os.path.join(d, "initial_prompt", "initial_prompt.md")) as fh:
            text = fh.read()
        self.assertIn("NO buildable Xcode project", text)

    def test_skips_app_that_is_not_done(self):
        d = self._make_app("appC", done=False, verify_ok=False)
        self._run_heredoc()
        self.assertFalse(os.path.exists(os.path.join(d, ".repair_pending")))

    def test_skips_app_with_successful_verify(self):
        d = self._make_app("appD", done=True, verify_ok=True)
        self._run_heredoc()
        self.assertFalse(os.path.exists(os.path.join(d, ".repair_pending")))

    def test_skips_app_already_repair_attempted(self):
        d = self._make_app("appE", done=True, verify_ok=False)
        open(os.path.join(d, ".repair_attempted"), "w").close()
        self._run_heredoc()
        self.assertFalse(os.path.exists(os.path.join(d, ".repair_pending")))

    def test_skips_dotdirs_and_batch_prefixed_dirs(self):
        for name in (".hidden", "batch-foo", "multi-app-exp1"):
            os.makedirs(os.path.join(self.root, name))
        proc = self._run_heredoc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_does_not_duplicate_change_requested_block(self):
        d = self._make_app("appF", done=True, verify_ok=False)
        prompt_path = os.path.join(d, "initial_prompt", "initial_prompt.md")
        with open(prompt_path, "a") as fh:
            fh.write("\n\n## Change requested\nalready here\n")
        self._run_heredoc()
        with open(prompt_path) as fh:
            text = fh.read()
        self.assertEqual(text.count("Change requested"), 1)


if __name__ == "__main__":
    unittest.main()
