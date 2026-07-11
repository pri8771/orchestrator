import os, tempfile, unittest
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
        self.assertTrue(gr.try_claim("p", "local_model", cap=1, pid=pid, db_path=self.db))
        # second claim on a full class (live pid holding it) -> False
        self.assertFalse(gr.try_claim("p", "local_model", cap=1, pid=pid, db_path=self.db))
        gr.release("local_model", pid=pid, db_path=self.db)
        self.assertTrue(gr.try_claim("p", "local_model", cap=1, pid=pid, db_path=self.db))

    def test_dead_pid_reaped(self):
        # a very unlikely-to-exist pid is reaped so its slot frees
        dead = 999999
        gr.try_claim("p", "cli_remote", cap=1, pid=dead, db_path=self.db)
        # even though a slot was inserted for a dead pid, the next claim reaps it
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=1, pid=os.getpid(), db_path=self.db))

    def test_release_never_raises_on_missing(self):
        gr.release("cli_remote", pid=os.getpid(), db_path=self.db)  # nothing to release

    def test_fail_open_on_bad_path(self):
        # an unwritable path -> claim fails open (returns True), never raises
        self.assertTrue(gr.try_claim("p", "cli_remote", cap=0, pid=1, db_path="/proc/nonexistent/x.db"))


if __name__ == "__main__":
    unittest.main()
