import json, os, tempfile, unittest
import verify


class TestVerifyPersistence(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

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


if __name__ == "__main__":
    unittest.main()
