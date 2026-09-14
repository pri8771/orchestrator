import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "wait-how-big-social/assets/reviewed/produce_batch.py"


class BatchIntegrityTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("whb_batch_under_test", SOURCE)
        assert spec is not None and spec.loader is not None
        self.batch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.batch)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch.ROOT = self.root
        self.batch.PACKET_PATH = self.root / "packet.json"
        self.batch.STATE_PATH = self.root / "state.jsonl"
        self.batch.STOP_FILE = self.root / "STOP"
        self.asset = self.root / "asset.bin"
        self.asset.write_bytes(b"approved artifact")
        self.packet(self.asset.read_bytes())

    def packet(self, content):
        digest = hashlib.sha256(content).hexdigest()
        self.batch.PACKET_PATH.write_text(json.dumps({"max_items": 1, "items": [
            {"content_id": "same-id", "sha256": digest, "relative_path": "asset.bin"}
        ]}), encoding="utf-8")

    def run_batch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.batch.main()
        receipt = json.loads((self.root / "S04_05_BATCH_RECEIPT.json").read_text())
        return code, receipt

    def test_unchanged_artifact_skips_duplicate(self):
        self.assertEqual(self.run_batch()[0], 0)
        code, receipt = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(receipt["skipped_duplicate_count"], 1)

    def test_same_id_tampering_is_rejected(self):
        self.assertEqual(self.run_batch()[0], 0)
        self.asset.write_bytes(b"unexpected replacement")
        code, receipt = self.run_batch()
        self.assertEqual(code, 1)
        self.assertEqual(receipt["skipped_duplicate_count"], 0)
        self.assertTrue(all(row["result"] == "hash_mismatch" for row in receipt["items"]))

    def test_new_expected_revision_is_checked(self):
        self.assertEqual(self.run_batch()[0], 0)
        self.asset.write_bytes(b"approved revised artifact")
        self.packet(self.asset.read_bytes())
        code, receipt = self.run_batch()
        self.assertEqual(code, 0)
        self.assertEqual(receipt["ok_count"], 1)
        self.assertEqual(receipt["skipped_duplicate_count"], 0)

    def test_stop_preserves_ledger(self):
        self.run_batch()
        before = self.batch.STATE_PATH.read_bytes()
        self.batch.STOP_FILE.write_text("stop")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.batch.main(), 0)
        self.assertEqual(self.batch.STATE_PATH.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
