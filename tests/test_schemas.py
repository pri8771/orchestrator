import unittest
import schemas


class TestExtractStructuredBlocks(unittest.TestCase):
    def test_valid_blocks_extracted(self):
        text = """noise
```task-json
{"id": "T-1", "title": "x", "owner_lane": "ui", "files": [], "status": "pending"}
```
more
```task-json
{"id": "T-2", "title": "y", "owner_lane": "core", "files": [], "status": "done"}
```"""
        errs = []
        out = schemas.extract_structured_blocks(
            text, "task-json", schemas.REQUIRED_FIELDS["task_item"], on_error=errs.append)
        self.assertEqual([o["id"] for o in out], ["T-1", "T-2"])
        self.assertEqual(errs, [])

    def test_malformed_json_reported_not_dropped_silently(self):
        text = "```finding-json\n{not valid json}\n```"
        errs = []
        out = schemas.extract_structured_blocks(text, "finding-json", [], on_error=errs.append)
        self.assertEqual(out, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("failed JSON parse", errs[0])

    def test_missing_required_field_reported(self):
        text = '```finding-json\n{"source": "audit", "category": "bug"}\n```'
        errs = []
        out = schemas.extract_structured_blocks(
            text, "finding-json", schemas.REQUIRED_FIELDS["finding"], on_error=errs.append)
        self.assertEqual(out, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("missing required field", errs[0])

    def test_non_object_reported(self):
        # A fenced top-level array must be captured and reported, not silently
        # dropped (the old regex required a leading `{` so it never matched).
        text = '```task-json\n[{"id": "T-1"}, {"id": "T-2"}]\n```'
        errs = []
        out = schemas.extract_structured_blocks(text, "task-json", [], on_error=errs.append)
        self.assertEqual(out, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("not a JSON object", errs[0])

    def test_unclosed_block_does_not_swallow_next_block(self):
        # A truncated block used to lazily match across the NEXT valid block of
        # the same fence, so neither parsed. Now: 1 error + 1 parsed block.
        text = ('```finding-json\n{"source": "audit", "category": "bug",\n'
                '```finding-json\n'
                '{"source": "audit", "category": "bug", "severity": "High", "title": "t"}\n'
                '```')
        errs = []
        out = schemas.extract_structured_blocks(
            text, "finding-json", schemas.REQUIRED_FIELDS["finding"], on_error=errs.append)
        self.assertEqual([o["title"] for o in out], ["t"])
        self.assertEqual(len(errs), 1)
        self.assertIn("no closing fence", errs[0])

    def test_truncated_block_at_end_of_text_reported(self):
        errs = []
        out = schemas.extract_structured_blocks(
            '```task-json\n{"id": "T-1"', "task-json", [], on_error=errs.append)
        self.assertEqual(out, [])
        self.assertEqual(len(errs), 1)
        self.assertIn("no closing fence", errs[0])

    def test_no_error_callback_never_raises(self):
        # Default on_error must not blow up on malformed input.
        out = schemas.extract_structured_blocks("```x\n{bad}\n```", "x", ["a"])
        self.assertEqual(out, [])


class TestValidateRequiredFields(unittest.TestCase):
    def test_ok(self):
        ok, missing = schemas.validate_required_fields({"a": 1, "b": 2}, ["a", "b"])
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_missing(self):
        ok, missing = schemas.validate_required_fields({"a": 1}, ["a", "b"])
        self.assertFalse(ok)
        self.assertEqual(missing, ["b"])

    def test_none_counts_as_missing(self):
        ok, missing = schemas.validate_required_fields({"a": None}, ["a"])
        self.assertFalse(ok)

    def test_non_dict(self):
        ok, missing = schemas.validate_required_fields("nope", ["a"])
        self.assertFalse(ok)


class TestNormalizers(unittest.TestCase):
    def test_severity_aliases(self):
        self.assertEqual(schemas.normalize_severity("medium"), "Med")
        self.assertEqual(schemas.normalize_severity("INFO"), "Low")
        self.assertEqual(schemas.normalize_severity("Critical"), "Critical")
        self.assertEqual(schemas.normalize_severity("garbage"), "Med")

    def test_verify_status(self):
        self.assertEqual(schemas.normalize_verify_status({"ran": True, "ok": True}), "verified")
        self.assertEqual(schemas.normalize_verify_status({"ran": True, "ok": False}), "failed")
        self.assertEqual(schemas.normalize_verify_status({"ran": False, "ok": False}), "unverified")
        self.assertEqual(schemas.normalize_verify_status(None), "unverified")


if __name__ == "__main__":
    unittest.main()


class TestLabeledFenceFallback(unittest.TestCase):
    def test_label_line_with_generic_json_fence(self):
        # Real-world drift (multi-app-exp3): the info string moved into a bold
        # label line above a plain ```json fence.
        text = ('We cleared the bar.\n\n**portfolio-json:**\n```json\n'
                '{"apps": [{"name": "A", "slug": "a", "build": true}]}\n```\n')
        errs = []
        blocks = schemas.extract_structured_blocks(text, "portfolio-json",
                                                   on_error=errs.append)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["apps"][0]["slug"], "a")
        self.assertEqual(errs, [])

    def test_fallback_not_used_when_strict_block_exists(self):
        text = ('```portfolio-json\n{"apps": [{"slug": "strict"}]}\n```\n'
                'portfolio-json:\n```json\n{"apps": [{"slug": "labeled"}]}\n```\n')
        blocks = schemas.extract_structured_blocks(text, "portfolio-json")
        self.assertEqual([b["apps"][0]["slug"] for b in blocks], ["strict"])
