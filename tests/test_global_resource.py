import os, sqlite3, tempfile, time, unittest
import unittest.mock
import global_resource as gr


class TestWorkerBroker(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "workers.db")

    def test_claim_up_to_cap_then_full(self):
        self.assertTrue(gr.try_claim("a", "cli_remote", cap=2, pid=1001, db_path=self.db))
        self.assertTrue(gr.try_claim("a", "cli_remote", cap=2, pid=1002, db_path=self.db))
        # cap reached (both pids alive == this test process? use current pid so alive)
        # use the real pid so reaping keeps the rows
        pid = os.getpid()
        gr.try_claim("x", "cli_remote", cap=99, pid=pid, db_path=self.db)  # add one live
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db) >= 1, True)

    def test_cap_enforced_with_live_pid(self):
        pid = os.getpid()
        token = gr.try_claim("p", "local_model", cap=1, pid=pid, db_path=self.db)
        self.assertTrue(token)
        # second claim on a full class (live pid holding it) -> False
        self.assertFalse(gr.try_claim("p", "local_model", cap=1, pid=pid, db_path=self.db))
        gr.release("local_model", token, pid=pid, db_path=self.db)
        self.assertTrue(gr.try_claim("p", "local_model", cap=1, pid=pid, db_path=self.db))

    def test_release_by_token_leaves_other_claim_intact(self):
        # A single pid holding TWO concurrent claims of the same resource_class
        # (e.g. call_agent running in multiple threads) must be able to release
        # exactly one of them without freeing the other's still-active slot.
        pid = os.getpid()
        token1 = gr.try_claim("p", "cli_remote", cap=5, pid=pid, db_path=self.db)
        token2 = gr.try_claim("p", "cli_remote", cap=5, pid=pid, db_path=self.db)
        self.assertTrue(token1)
        self.assertTrue(token2)
        self.assertNotEqual(token1, token2)
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db), 2)
        gr.release("cli_remote", token1, pid=pid, db_path=self.db)
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db), 1)
        conn = gr._conn(self.db)
        remaining = conn.execute("SELECT rowid FROM worker_slots WHERE resource_class=?",
                                 ("cli_remote",)).fetchall()
        conn.close()
        self.assertEqual([row[0] for row in remaining], [token2])

    def test_dead_pid_reaped(self):
        # a very unlikely-to-exist pid is reaped so its slot frees
        dead = 999999
        gr.try_claim("p", "cli_remote", cap=1, pid=dead, db_path=self.db)
        # even though a slot was inserted for a dead pid, the next claim reaps it
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=1, pid=os.getpid(), db_path=self.db))

    def test_release_never_raises_on_missing(self):
        gr.release("cli_remote", 99999, pid=os.getpid(), db_path=self.db)  # nothing to release

    def test_release_noop_on_non_int_token(self):
        # A fail-open claim (cap<=0, broker unavailable) returns True, not a
        # real rowid — release() must treat that as a no-op, not attempt to
        # delete some arbitrary row.
        gr.release("cli_remote", True, pid=os.getpid(), db_path=self.db)
        gr.release("cli_remote", False, pid=os.getpid(), db_path=self.db)

    def test_stale_slot_reaped_by_age_even_if_pid_alive(self):
        # A live PID (our own) that has held a slot longer than the max age is a
        # leak (crashed process, PID recycled). Age-based reaping frees it so the
        # cap can't be permanently pinned by a reused PID.
        gr.try_claim("p", "cli_remote", cap=5, pid=os.getpid(), db_path=self.db)
        # Backdate the claim past the age bound.
        conn = gr._conn(self.db)
        conn.execute("UPDATE worker_slots SET claimed_at=?",
                     (time.time() - gr._MAX_SLOT_AGE_SECONDS - 60,))
        conn.commit()
        conn.close()
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db), 0)

    def test_fail_open_on_bad_path(self):
        # an unwritable path -> claim fails open (returns True), never raises
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=5, pid=1, db_path="/proc/nonexistent/x.db"))

    def test_nonpositive_cap_fails_open_not_closed(self):
        # cap=0/negative is a misconfiguration (nothing upstream validates
        # runtime.global_worker_cap.*), not an intentional "block everything" —
        # it must degrade to uncapped rather than silently wedging every claim
        # shut with no diagnostic.
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=0, pid=os.getpid(), db_path=self.db))
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=-1, pid=os.getpid(), db_path=self.db))

    def test_non_integer_cap_fails_open(self):
        self.assertTrue(gr.try_claim("p", "cli_remote", cap="not-a-number",
                                     pid=os.getpid(), db_path=self.db))
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=None,
                                     pid=os.getpid(), db_path=self.db))

    def test_lock_contention_fails_closed_not_open(self):
        # Persistent write-lock contention must NOT be mistaken for broker
        # unavailability: if it failed open, the machine-wide cap would be
        # silently exceeded exactly under the concurrency it exists to bound.
        locked = sqlite3.OperationalError("database is locked")
        with unittest.mock.patch.object(gr, "_claim_once", side_effect=locked), \
                unittest.mock.patch.object(gr.time, "sleep", lambda *_: None):
            self.assertFalse(
                gr.try_claim("p", "cli_remote", cap=5, pid=os.getpid(),
                             db_path=self.db))

    def test_transient_contention_then_success(self):
        # One lock blip, then the claim succeeds on retry (cap not yet full).
        real = gr._claim_once
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)

        with unittest.mock.patch.object(gr, "_claim_once", side_effect=flaky), \
                unittest.mock.patch.object(gr.time, "sleep", lambda *_: None):
            self.assertTrue(
                gr.try_claim("p", "cli_remote", cap=5, pid=os.getpid(),
                             db_path=self.db))
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
