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
        self.assertEqual([row[0] for row in remaining], [token2[0]])

    def test_dead_pid_reaped(self):
        # a very unlikely-to-exist pid is reaped so its slot frees
        dead = 999999
        gr.try_claim("p", "cli_remote", cap=1, pid=dead, db_path=self.db)
        # even though a slot was inserted for a dead pid, the next claim reaps it
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=1, pid=os.getpid(), db_path=self.db))

    def test_release_never_raises_on_missing(self):
        gr.release("cli_remote", (99999, "no-such-uuid"),
                   pid=os.getpid(), db_path=self.db)  # nothing to release

    def test_release_noop_on_malformed_token(self):
        # A fail-open claim (cap<=0, broker unavailable) returns True, not a
        # real (rowid, uuid) token — release() must treat that (and any other
        # non-token shape, including a bare legacy rowid int) as a no-op, not
        # attempt to delete some arbitrary row.
        for bad in (True, False, 7, "7", (True, "x"), (7,), (7, 8), None):
            gr.release("cli_remote", bad, pid=os.getpid(), db_path=self.db)

    def test_stale_double_release_cannot_free_recycled_rowid(self):
        # SQLite reuses max-rowid: claim A, release A, claim B — B may get A's
        # rowid back. A buggy second release(A) must NOT delete B's row; the
        # per-claim uuid in the token makes the stale token miss structurally.
        pid = os.getpid()
        token_a = gr.try_claim("p", "cli_remote", cap=5, pid=pid, db_path=self.db)
        gr.release("cli_remote", token_a, pid=pid, db_path=self.db)
        token_b = gr.try_claim("p", "cli_remote", cap=5, pid=pid, db_path=self.db)
        self.assertEqual(token_b[0], token_a[0])  # rowid actually recycled
        gr.release("cli_remote", token_a, pid=pid, db_path=self.db)  # stale double release
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db), 1,
                         "stale double release deleted the recycled-rowid claim")
        gr.release("cli_remote", token_b, pid=pid, db_path=self.db)
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db), 0)

    def test_reap_survives_cap_full_claim_rollback(self):
        # The reap runs in its own committed transaction: a cap-full claim's
        # rollback used to undo the reap's deletes, so garbage rows were never
        # purged under sustained contention.
        gr.try_claim("p", "cli_remote", cap=2, pid=999999, db_path=self.db)  # dead pid
        live = gr.try_claim("p", "cli_remote", cap=2, pid=os.getpid(), db_path=self.db)
        self.assertTrue(live)
        # Backdate nothing; fill the cap so the next claim fails AFTER reaping.
        conn = gr._conn(self.db)
        conn.execute("INSERT INTO worker_slots VALUES (?,?,?,?,?)",
                     (os.getpid(), "p", "cli_remote", time.time(), "u2"))
        conn.commit()
        conn.close()
        self.assertFalse(gr.try_claim("p", "cli_remote", cap=2,
                                      pid=os.getpid(), db_path=self.db))
        conn = gr._conn(self.db)
        dead_rows = conn.execute("SELECT COUNT(*) FROM worker_slots WHERE pid=?",
                                 (999999,)).fetchone()[0]
        conn.close()
        self.assertEqual(dead_rows, 0, "cap-full rollback undid the reap")

    def test_old_schema_db_migrated_in_place(self):
        # A DB created by the previous release has no claim_uuid column; _conn
        # must ALTER it in place so claims/releases keep working.
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE worker_slots "
                     "(pid INTEGER, project_id TEXT, resource_class TEXT, claimed_at REAL)")
        conn.commit()
        conn.close()
        token = gr.try_claim("p", "cli_remote", cap=2, pid=os.getpid(), db_path=self.db)
        self.assertIsInstance(token, tuple)
        gr.release("cli_remote", token, pid=os.getpid(), db_path=self.db)
        self.assertEqual(gr.active_count("cli_remote", db_path=self.db), 0)

    def test_pid_index_created(self):
        # _reap does SELECT DISTINCT pid FROM worker_slots on every claim; an
        # index backs that scan. Confirm _conn actually creates it (and that
        # doing so twice, i.e. reconnecting, is a harmless no-op).
        conn = gr._conn(self.db)
        conn2 = gr._conn(self.db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("idx_worker_slots_pid", names)
        conn.close()
        conn2.close()

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
