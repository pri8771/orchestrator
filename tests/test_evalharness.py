"""Eval harness (evalharness.py): scorecard plumbing. Coverage for
_events_span's timestamp handling — a long-lived project's events.jsonl can
mix the naive local timestamps of the pre-tz-change era with today's aware
local+offset ones (A-53), and the span must degrade, never crash the whole
--eval-report run."""
import json
import os
import tempfile
import unittest

import evalharness as ev


def _write_events(rows):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "events.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return d


class TestEventsSpan(unittest.TestCase):
    def test_missing_file_is_zero(self):
        self.assertEqual(ev._events_span(tempfile.mkdtemp()), (0, 0))

    def test_uniform_aware_timestamps(self):
        d = _write_events([
            {"ts": "2026-07-28T10:00:00-04:00"},
            {"ts": "2026-07-28T10:05:00-04:00", "kind": "turn_completed"}])
        self.assertEqual(ev._events_span(d), (300, 1))

    def test_mixed_naive_and_aware_yields_span_not_typeerror(self):
        # A-53: naive minus aware raises TypeError (not ValueError), which
        # escaped the old `except ValueError` and crashed score_project. Both
        # eras are local wall clock, so the mixed pair still yields a span.
        d = _write_events([
            {"ts": "2026-07-20T10:00:00"},
            {"ts": "2026-07-28T11:00:00-04:00"}])
        wall, turns = ev._events_span(d)
        self.assertEqual(wall, 8 * 86400 + 3600)
        self.assertEqual(turns, 0)

    def test_unparseable_timestamps_degrade_to_zero_span(self):
        d = _write_events([
            {"ts": "not a time"},
            {"ts": "also not", "kind": "turn_completed"}])
        self.assertEqual(ev._events_span(d), (0, 1))


if __name__ == "__main__":
    unittest.main()
