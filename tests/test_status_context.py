"""E7 status-aware chat context uses persisted facts, never guesses."""

import json
import os
import shutil
import tempfile
import unittest

import costs
import orchestrator as orch
import statuscontext
import workflows


class TestStatusContext(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="enroll-e7-")
        self.addCleanup(shutil.rmtree, self.app_dir, True)

    def write_json(self, name, value):
        with open(os.path.join(self.app_dir, name), "w", encoding="utf-8") as fh:
            json.dump(value, fh)

    def test_block_is_truthful_and_tails_exactly_ten_parseable_events(self):
        self.write_json("agent_state.json", {
            "status": "running", "current_phase": "build_coordination",
            "completed_phases": ["one", "two"],
        })
        costs.record_turn(self.app_dir, {
            "v": 1, "ts": "now", "agent": "codex", "metered": True,
            "input_tokens": 10, "output_tokens": 20,
            "cost_micro_usd": 1230000,
        })
        with open(os.path.join(self.app_dir, "events.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write("not-json\n")
            for index in range(12):
                fh.write(json.dumps({"kind": "phase_started",
                                     "phase": "phase-%d" % index}) + "\n")
        block = statuscontext.render(self.app_dir)
        self.assertIn("Status: running", block)
        self.assertIn("Current phase: build_coordination", block)
        self.assertIn("Completed phases: 2", block)
        self.assertIn("Recorded cost: $1.23", block)
        rendered = block + "\n"
        self.assertNotIn("- phase_started — phase=phase-0\n", rendered)
        self.assertNotIn("- phase_started — phase=phase-1\n", rendered)
        for index in range(2, 12):
            self.assertIn("- phase_started — phase=phase-%d\n" % index,
                          rendered)

    def test_absent_or_corrupt_fields_are_unknown_never_invented(self):
        self.write_json("agent_state.json", {"completed_phases": "not-a-list"})
        with open(os.path.join(self.app_dir, "events.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write("{broken\n")
        block = statuscontext.render(self.app_dir)
        self.assertIn("Status: unknown", block)
        self.assertIn("Current phase: unknown", block)
        self.assertIn("Completed phases: unknown", block)
        self.assertIn("Recorded cost: unknown", block)
        self.assertIn("- none observed", block)
        self.assertNotIn("Status: running", block)

    def test_context_injects_for_chat_and_answer_but_not_ordinary_phase(self):
        self.write_json("agent_state.json", {
            "status": "awaiting_human", "current_phase": "chat",
            "completed_phases": [],
        })
        chat = workflows.load_workflow("chat_ideas").phases[0]
        cfg = {"_app_dir": self.app_dir, "_workflow_name": "chat_ideas",
               "_workflow_target": "app", "runtime": {}}
        chat_ctx = orch.build_context(cfg, "project", chat, "prompt", [], "")
        self.assertIn("CURRENT PROJECT STATUS", chat_ctx)
        self.assertIn("Status: awaiting_human", chat_ctx)

        answer = workflows.load_workflow("answer_question").phases[0]
        answer_cfg = {"_app_dir": self.app_dir,
                      "_workflow_name": "answer_question",
                      "_workflow_target": "answer", "runtime": {}}
        self.assertIn("CURRENT PROJECT STATUS", orch.build_context(
            answer_cfg, "project", answer, "prompt", [], ""))

        ordinary = workflows.load_workflow("iterate").phases[0]
        ordinary_cfg = {"_app_dir": self.app_dir, "_workflow_name": "iterate",
                        "_workflow_target": "app", "runtime": {}}
        self.assertNotIn("CURRENT PROJECT STATUS", orch.build_context(
            ordinary_cfg, "project", ordinary, "prompt", [], ""))


if __name__ == "__main__":
    unittest.main()
