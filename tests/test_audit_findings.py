"""Audit finding-block parsing/dedup/rank (orchestrator.py).

Regression focus: parse_finding_blocks must survive nested braces and multiple
blocks. The former lazy ```finding-json\\s*(\\{.*?\\})``` regex truncated a body
at its first inner `}` and swallowed text across an unclosed fence, silently
dropping real findings; parsing now goes through schemas.extract_structured_blocks.
"""
import unittest

import orchestrator as orch


def _block(obj_json):
    return "```finding-json\n%s\n```" % obj_json


class TestParseFindingBlocks(unittest.TestCase):
    def test_multiple_blocks_all_parsed(self):
        text = "\n".join([
            _block('{"title":"A","severity":"High","category":"bug","file":"a.py"}'),
            "some prose between blocks",
            _block('{"title":"B","severity":"Critical","category":"security","file":"b.py"}'),
        ])
        got = orch.parse_finding_blocks(text)
        self.assertEqual(sorted(f["title"] for f in got), ["A", "B"])

    def test_nested_braces_not_truncated(self):
        # The old regex captured only up to the first `}` (inside "meta"),
        # producing invalid JSON and dropping the finding entirely.
        text = _block('{"title":"C","category":"bug","file":"z.py",'
                      '"meta":{"a":1,"b":[2,3]}}')
        got = orch.parse_finding_blocks(text)
        self.assertEqual([f["title"] for f in got], ["C"])

    def test_normalizes_severity_confidence_category(self):
        text = _block('{"title":"D","severity":"Moderate","confidence":"HIGH",'
                      '"category":"nonsense","file":"d.py"}')
        got = orch.parse_finding_blocks(text)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["severity"], "Med")       # Moderate -> Med
        self.assertEqual(got[0]["confidence"], "high")
        self.assertEqual(got[0]["category"], "bug")        # unknown -> bug

    def test_normalizes_source(self):
        text = _block('{"title":"E","category":"bug","file":"e.py","source":"CODE_REVIEW"}')
        got = orch.parse_finding_blocks(text)
        self.assertEqual(got[0]["source"], "code_review")
        bad = _block('{"title":"F","category":"bug","file":"f.py","source":"made-up"}')
        self.assertEqual(orch.parse_finding_blocks(bad)[0]["source"], "audit")

    def test_titleless_block_skipped(self):
        text = _block('{"severity":"High","category":"bug","file":"e.py"}')
        self.assertEqual(orch.parse_finding_blocks(text), [])

    def test_garbage_never_raises(self):
        for bad in ("", None, "```finding-json\n{not json}\n```",
                    "```finding-json\n{\"title\":\"x\"", "no fences here"):
            orch.parse_finding_blocks(bad)  # must not raise

    def test_dedup_keeps_higher_confidence(self):
        text = "\n".join([
            _block('{"title":"Same bug","category":"bug","file":"f.py",'
                   '"confidence":"low","fix":"short"}'),
            _block('{"title":"Same bug","category":"bug","file":"f.py",'
                   '"confidence":"high","fix":"a much longer and better fix"}'),
        ])
        deduped = orch.dedup_findings(orch.parse_finding_blocks(text))
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["confidence"], "high")

    def test_rank_orders_by_severity_then_confidence(self):
        text = "\n".join([
            _block('{"title":"low","severity":"Low","category":"bug","file":"a"}'),
            _block('{"title":"crit","severity":"Critical","category":"security","file":"b"}'),
            _block('{"title":"high","severity":"High","category":"bug","file":"c"}'),
        ])
        ranked = orch.rank_findings(orch.parse_finding_blocks(text))
        self.assertEqual([f["title"] for f in ranked][0], "crit")


if __name__ == "__main__":
    unittest.main()
