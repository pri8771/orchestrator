import os
import tempfile
import time
import unittest

import orchestrator as orch


class TestLockHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_locks_dir = orch.LOCKS_DIR
        self._orig_pid_alive = orch._pid_alive
        self._orig_held = set(orch._HELD_LOCKS)

        orch.LOCKS_DIR = os.path.join(self.tmp, "locks")

    def tearDown(self):
        orch._pid_alive = self._orig_pid_alive
        orch.LOCKS_DIR = self._orig_locks_dir
        orch._HELD_LOCKS.clear()
        orch._HELD_LOCKS.update(self._orig_held)
        lock_file = os.path.join(self.tmp, "locks")
        if os.path.isdir(lock_file):
            for name in os.listdir(lock_file):
                try:
                    os.remove(os.path.join(lock_file, name))
                except OSError:
                    pass
            try:
                os.rmdir(lock_file)
            except OSError:
                pass

    def _write_lock(self, path, pid, age_seconds=0):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("pid=%d host=test started=2000-01-01T00:00:00Z\n" % pid)
        old = time.time() - age_seconds
        os.utime(path, (old, old))

    # (The legacy machine-wide global lock — acquire_lock/release_lock/LOCK_PATH —
    # was removed; locking is per-app. Those tests were removed with it.)

    def test_app_lock_reclaims_dead_owner(self):
        p = os.path.join(orch.LOCKS_DIR, "app-a.lock")
        self._write_lock(p, pid=999997, age_seconds=10)
        orch._pid_alive = lambda _pid: False
        got = orch.acquire_app_lock("app-a", stale_seconds=3600)
        self.assertTrue(got)
        self.assertIn(p, orch._HELD_LOCKS)
        orch.release_app_lock("app-a")

    def test_app_lock_blocks_live_owner(self):
        p = os.path.join(orch.LOCKS_DIR, "app-b.lock")
        self._write_lock(p, pid=999996, age_seconds=10)
        orch._pid_alive = lambda _pid: True
        got = orch.acquire_app_lock("app-b", stale_seconds=3600)
        self.assertFalse(got)

    def test_app_lock_blocks_live_stale_orchestrator(self):
        # A real orchestrator that merely missed heartbeats (machine slept)
        # must NOT lose its lock, no matter how old the mtime is.
        p = os.path.join(orch.LOCKS_DIR, "app-d.lock")
        self._write_lock(p, pid=999995, age_seconds=99999)
        orch._pid_alive = lambda _pid: True
        orig = orch._pid_looks_like_orchestrator
        orch._pid_looks_like_orchestrator = lambda _pid: True
        try:
            self.assertFalse(orch.acquire_app_lock("app-d", stale_seconds=3600))
        finally:
            orch._pid_looks_like_orchestrator = orig

    def test_app_lock_reclaims_live_stale_recycled_pid(self):
        # Stale lock whose pid now belongs to an unrelated process: reclaim.
        p = os.path.join(orch.LOCKS_DIR, "app-e.lock")
        self._write_lock(p, pid=999994, age_seconds=99999)
        orch._pid_alive = lambda _pid: True
        orig = orch._pid_looks_like_orchestrator
        orch._pid_looks_like_orchestrator = lambda _pid: False
        try:
            self.assertTrue(orch.acquire_app_lock("app-e", stale_seconds=3600))
        finally:
            orch._pid_looks_like_orchestrator = orig
        orch.release_app_lock("app-e")

    def test_release_app_lock_leaves_foreign_lock(self):
        # After a reclaim, the path may belong to a newer run — release must
        # not delete a lock recording someone else's pid.
        p = os.path.join(orch.LOCKS_DIR, "app-f.lock")
        self._write_lock(p, pid=999993, age_seconds=10)
        orch.release_app_lock("app-f")
        self.assertTrue(os.path.exists(p))
        os.remove(p)

    def test_app_lock_exclusive_while_held(self):
        # Second acquire in the same process must lose: the holder's pid is
        # alive and the lock is fresh. This is the one-run-per-app guarantee.
        self.assertTrue(orch.acquire_app_lock("app-c", stale_seconds=3600))
        self.assertFalse(orch.acquire_app_lock("app-c", stale_seconds=3600))
        orch.release_app_lock("app-c")

    def test_run_heartbeat_refreshes_lock_and_state_mtimes(self):
        app_dir = os.path.join(self.tmp, "app-hb")
        os.makedirs(app_dir, exist_ok=True)
        self.assertTrue(orch.acquire_app_lock("app-hb", stale_seconds=3600))
        state_p = orch.state_path(app_dir)
        with open(state_p, "w", encoding="utf-8") as fh:
            fh.write("{}")
        lock_p = orch._app_lock_path("app-hb")
        old = time.time() - 3600
        for p in (lock_p, state_p):
            os.utime(p, (old, old))
        stop = orch._start_run_heartbeat("app-hb", app_dir, interval=0.05)
        try:
            deadline = time.time() + 5
            while time.time() < deadline and (
                    os.path.getmtime(lock_p) <= old + 1
                    or os.path.getmtime(state_p) <= old + 1):
                time.sleep(0.05)
        finally:
            stop.set()
        # A long agent call must not let the lock/state look stale (GUI freshness
        # window + stale-lock reclaim both key off these mtimes).
        self.assertGreater(os.path.getmtime(lock_p), old + 1)
        self.assertGreater(os.path.getmtime(state_p), old + 1)
        orch.release_app_lock("app-hb")


if __name__ == "__main__":
    unittest.main()
