import json, os, tempfile, threading, unittest
import verify


class TestVerifyPersistence(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_concurrent_persist_keeps_all_records(self):
        # The load-append-replace is guarded by an flock, so parallel writers
        # (portfolio children / build-worker threads) don't lose records to a
        # last-write-wins race.
        n = 40
        barrier = threading.Barrier(n)

        def writer(i):
            barrier.wait()  # maximize overlap on the read-modify-write
            verify.persist_verify_result(
                self.d, "phase-%d" % i,
                {"ran": True, "ok": True, "tool": "t"}, attempt=i)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = verify.load_verify_results(self.d)
        self.assertEqual(len(recs), n)
        self.assertEqual(sorted(r["phase"] for r in recs),
                         sorted("phase-%d" % i for i in range(n)))

    def test_server_log_not_left_in_build_dir(self):
        # A boot attempt that can't start anything must not drop a
        # .verify_server.log into the built project tree.
        before = set(os.listdir(self.d))
        verify._verify_http(self.d, {"start": "this-command-does-not-exist-xyz",
                                     "ready_timeout": 1}, timeout=2)
        after = set(os.listdir(self.d))
        self.assertNotIn(".verify_server.log", after)
        self.assertEqual(after, before)   # nothing dropped into the project tree

    def test_status_mapping(self):
        self.assertEqual(verify.verification_status({"ran": True, "ok": True}), "verified")
        self.assertEqual(verify.verification_status({"ran": True, "ok": False}), "failed")
        self.assertEqual(verify.verification_status({"ran": False, "ok": False}), "unverified")

    def test_append_order_and_attempts(self):
        verify.persist_verify_result(self.d, "build", {"ran": True, "ok": False, "tool": "xcodebuild"}, attempt=0)
        verify.persist_verify_result(self.d, "build", {"ran": True, "ok": True, "tool": "xcodebuild"}, attempt=1)
        recs = verify.load_verify_results(self.d)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["attempt"], 0)
        self.assertFalse(recs[0]["repair_attempt"])
        self.assertEqual(recs[1]["attempt"], 1)
        self.assertTrue(recs[1]["repair_attempt"])
        self.assertEqual(recs[0]["status"], "failed")
        self.assertEqual(recs[1]["status"], "verified")

    def test_latest_by_prompt_hash(self):
        verify.persist_verify_result(self.d, "build", {"ran": True, "ok": False, "tool": "x"}, attempt=0, prompt_hash="AAA")
        verify.persist_verify_result(self.d, "build", {"ran": True, "ok": True, "tool": "x"}, attempt=1, prompt_hash="AAA")
        latest = verify.latest_verify_result(self.d, prompt_hash="AAA")
        self.assertEqual(latest["status"], "verified")
        self.assertIsNone(verify.latest_verify_result(self.d, prompt_hash="ZZZ"))

    def test_atomic_write_valid_json(self):
        verify.persist_verify_result(self.d, "p", {"ran": False, "ok": False, "tool": "none"})
        with open(os.path.join(self.d, "verify_results.json")) as fh:
            json.load(fh)  # raises if not valid

    def test_missing_file_returns_empty(self):
        self.assertEqual(verify.load_verify_results(tempfile.mkdtemp()), [])

    def test_timestamp_is_timezone_aware_isoformat(self):
        # Matches events.py's pattern so record ordering against events.jsonl
        # stays unambiguous across DST transitions (naive datetime.now() was
        # ambiguous for one hour a year).
        import datetime
        rec = verify.persist_verify_result(self.d, "p",
                                           {"ran": True, "ok": True, "tool": "t"})
        parsed = datetime.datetime.fromisoformat(rec["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main()
