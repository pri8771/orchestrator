"""Tests for shepherd.sh's locked() staleness check via the --check-lock hook.

shepherd.sh is the external bash daemon that keeps the fleet running and resumes
interrupted builds. Its locked() gate used to be a bare `[ -f lock ]` file-
existence check, so a STALE lock — left by a build whose process crashed/was
killed without cleaning up on SIGTERM — made the app look forever-running:
shepherd never relaunched it and looped "N child app(s) still pending" forever.
locked() now checks the lock's pid is actually alive. Exercised through the real
script (`bash shepherd.sh --check-lock <app>`), like test_run_sh.py drives run.sh.
"""
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SHEPHERD = os.path.join(REPO_ROOT, "shepherd.sh")


def _has_bash():
    return shutil.which("bash") is not None


@unittest.skipUnless(_has_bash(), "requires bash")
class TestShepherdLockedStaleness(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="shep_lock_")
        os.makedirs(os.path.join(self.root, ".orch-locks"))
        self._procs = []

    def tearDown(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_lock(self, app, pid):
        with open(os.path.join(self.root, ".orch-locks", "%s.lock" % app), "w") as fh:
            fh.write("pid=%s host=test started=2026-07-15 12:00:00\n" % pid)

    def _check_lock(self, app):
        env = dict(os.environ)
        env["ORCH_ROOT"] = self.root
        return subprocess.run(["bash", SHEPHERD, "--check-lock", app],
                              cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=30, env=env).returncode

    def _live_pid(self):
        # A real, running child process whose pid is genuinely alive.
        p = subprocess.Popen(["sleep", "60"])
        self._procs.append(p)
        return p.pid

    def _dead_pid(self):
        # Spawn then reap a process so its pid is guaranteed not alive.
        p = subprocess.Popen(["sleep", "30"])
        p.terminate()
        p.wait(timeout=5)
        return p.pid

    def test_live_pid_lock_is_locked(self):
        self._write_lock("liveapp", self._live_pid())
        self.assertEqual(self._check_lock("liveapp"), 0)   # 0 = locked

    def test_stale_dead_pid_lock_is_not_locked(self):
        # The core fix: a crashed run's leftover lock must read as FREE so the
        # app is relaunched instead of looping "pending" forever.
        self._write_lock("deadapp", self._dead_pid())
        self.assertEqual(self._check_lock("deadapp"), 1)   # 1 = free/stale

    def test_absent_lock_is_not_locked(self):
        self.assertEqual(self._check_lock("nolock"), 1)

    def test_malformed_lock_without_pid_is_not_locked(self):
        with open(os.path.join(self.root, ".orch-locks", "badapp.lock"), "w") as fh:
            fh.write("garbage with no pid field\n")
        self.assertEqual(self._check_lock("badapp"), 1)

    def test_check_lock_hook_does_not_enter_fleet_loop(self):
        # The hook must exit promptly, never fall through into `while true`.
        env = dict(os.environ)
        env["ORCH_ROOT"] = self.root
        start = time.time()
        proc = subprocess.run(["bash", SHEPHERD, "--check-lock", "whatever"],
                              cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=15, env=env)
        self.assertLess(time.time() - start, 10)
        self.assertIn(proc.returncode, (0, 1))


@unittest.skipUnless(_has_bash(), "requires bash")
class TestShepherdAutorunDisabled(unittest.TestCase):
    """autorun_disabled() — the shared guard EVERY launch path (children AND the
    release-gate repair loop) must honor. Its absence from the repair loop let a
    .repair_pending marker relaunch an app the user had deliberately parked."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="shep_dis_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _check_disabled(self, app):
        env = dict(os.environ)
        env["ORCH_ROOT"] = self.root
        return subprocess.run(["bash", SHEPHERD, "--check-disabled", app],
                              cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=30, env=env).returncode

    def test_disabled_marker_is_detected(self):
        os.makedirs(os.path.join(self.root, "appA"))
        open(os.path.join(self.root, "appA", ".orchestrator_autorun_disabled"), "w").close()
        self.assertEqual(self._check_disabled("appA"), 0)   # 0 = disabled

    def test_enabled_app_is_not_disabled(self):
        os.makedirs(os.path.join(self.root, "appB"))
        self.assertEqual(self._check_disabled("appB"), 1)   # 1 = not disabled

    def test_missing_app_is_not_disabled(self):
        self.assertEqual(self._check_disabled("ghost"), 1)

    def test_repair_loop_now_references_the_shared_guard(self):
        # Regression guard: the repair loop (launches .repair_pending apps) must
        # call autorun_disabled — its omission was the bug. Assert the guard is
        # present in the repair-loop stanza so a future edit can't silently drop
        # it back to launching parked apps on a repair marker.
        with open(SHEPHERD, encoding="utf-8") as fh:
            src = fh.read()
        repair_stanza = src.split(".repair_pending", 1)
        self.assertGreater(len(repair_stanza), 1)
        # Within a few lines of the .repair_pending check, autorun_disabled fires.
        window = src[src.index(".repair_pending"): src.index(".repair_pending") + 400]
        self.assertIn("autorun_disabled", window)


if __name__ == "__main__":
    unittest.main()
