import json, os, tempfile, unittest
import orchestrator as orch
import schemas


class TestLiveLog(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.path = os.path.join(self.app_dir, "live_log.jsonl")

    def _lines(self):
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh.read().splitlines() if l.strip()]

    def test_entries_appended_with_required_fields(self):
        orch.live_log(self.app_dir, "tech_specs", "codex", "agent_turn_completed",
                      "decided the data model")
        orch.live_log(self.app_dir, "build_coordination", "orchestrator",
                      "phase_started", "phase started")
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        for entry in lines:
            for field in schemas.REQUIRED_FIELDS["live_log_entry"]:
                self.assertIn(field, entry)
                self.assertTrue(entry[field])
            self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(lines[0]["lane"], "tech_specs")
        self.assertEqual(lines[1]["kind"], "phase_started")

    def test_summary_redacted_and_capped(self):
        token = "sk-" + "abc123" * 4  # obviously fake, matches the openai shape
        orch.live_log(self.app_dir, "l", "a", "k", "the key is %s ok" % token)
        orch.live_log(self.app_dir, "l", "a", "k", "x" * 1000)
        lines = self._lines()
        self.assertNotIn(token, lines[0]["summary"])
        self.assertIn("[REDACTED:openai_key]", lines[0]["summary"])
        self.assertLessEqual(len(lines[1]["summary"]), 280)

    def test_summary_whitespace_collapsed_to_one_line(self):
        orch.live_log(self.app_dir, "l", "a", "k", "multi\nline\n\tsummary")
        self.assertEqual(self._lines()[0]["summary"], "multi line summary")

    def test_best_effort_never_raises(self):
        # Missing directory -> open fails -> swallowed, run keeps going.
        orch.live_log(os.path.join(self.app_dir, "no", "such", "dir"),
                      "l", "a", "k", "s")
        # Non-string junk is coerced, not fatal.
        orch.live_log(self.app_dir, None, None, None, {"weird": object()})
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
