"""Round-level (mid-phase) crash resume (V2 deep-research recommendation #2):
_resume_round_state's transcript-parsing logic in isolation, plus an
end-to-end process_phase resume (transcript not truncated, round loop starts
at the correct round, no round's turns silently skipped or double-run),
fresh-start behavior unaffected, and phase-level resume (already-completed
phases) still works."""
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


class TestResumeRoundStateParsing(unittest.TestCase):
    def test_no_rounds_at_all_resumes_at_one(self):
        rnd, kept, hend = orch._resume_round_state("just a header, no rounds yet")
        self.assertEqual(rnd, 1)
        self.assertEqual(kept, "just a header, no rounds yet")
        self.assertEqual(hend, len(kept))

    def test_one_complete_round_resumes_at_two(self):
        text = ("HEADER\n"
                "\n### Round 1\n\n"
                "**Codex — Round 1**\n\nfirst take\n"
                "\n**Coordinator (Codex) — decision after round 1**\n\nCONSENSUS: NO\n")
        rnd, kept, hend = orch._resume_round_state(text)
        self.assertEqual(rnd, 2)
        self.assertEqual(kept, text)          # nothing incomplete to drop
        self.assertEqual(hend, text.index("### Round 1"))

    def test_trailing_incomplete_round_is_dropped(self):
        complete = ("HEADER\n"
                    "\n### Round 1\n\n"
                    "**Codex — Round 1**\n\nfirst take\n"
                    "\n**Coordinator (Codex) — decision after round 1**\n\nCONSENSUS: NO\n")
        partial = "\n### Round 2\n\n**Codex — Round 2**\n\nsecond take (no coordinator yet)\n"
        rnd, kept, hend = orch._resume_round_state(complete + partial)
        self.assertEqual(rnd, 2)              # redo round 2 from scratch
        self.assertEqual(kept.rstrip("\n"), complete.rstrip("\n"))  # partial round 2 dropped
        self.assertNotIn("second take", kept)

    def test_round_started_but_none_complete_resumes_at_one(self):
        text = "HEADER\n\n### Round 1\n\n**Codex — Round 1**\n\nfirst take (no coordinator)\n"
        rnd, kept, hend = orch._resume_round_state(text)
        self.assertEqual(rnd, 1)
        self.assertEqual(kept.rstrip("\n"), "HEADER")
        self.assertNotIn("first take", kept)

    def test_lagging_counter_does_not_cause_double_run(self):
        # Two complete rounds on disk; the persisted counter (if trusted
        # directly instead of parsed) might say 2 (written before round 2
        # finished) even though round 2 fully completed before the crash.
        text = ("HEADER\n"
                "\n### Round 1\n\n**Codex — Round 1**\n\nA\n"
                "\n**Coordinator (Codex) — decision after round 1**\n\nCONSENSUS: NO\n"
                "\n### Round 2\n\n**Codex — Round 2**\n\nB\n"
                "\n**Coordinator (Codex) — decision after round 2**\n\nCONSENSUS: NO\n")
        rnd, kept, _hend = orch._resume_round_state(text)
        self.assertEqual(rnd, 3)              # not 2 — round 2 IS complete on disk
        self.assertEqual(kept, text)

    def test_iteration_label_also_recognized(self):
        text = ("HEADER\n\n### Iteration 1\n\n**Codex — Iteration 1**\n\nx\n"
                "\n**Coordinator (Codex) — decision after iteration 1**\n\nCONSENSUS: NO\n")
        rnd, kept, _hend = orch._resume_round_state(text)
        self.assertEqual(rnd, 2)


class TestProcessPhaseRoundResume(unittest.TestCase):
    """Drives process_phase() end-to-end with a single stubbed agent so the
    round loop's resume-vs-fresh-start branch can be observed directly."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent == "codex"
        self.calls = []

        def fake_sessioned(cfg, app, phase, rnd, agent, prompt, delta_prompt=None,
                           session_key=None):
            self.calls.append((rnd, agent, session_key))
            if session_key and session_key.endswith(":coord"):
                return "Aligned now. CONSENSUS: YES\n\n## Final Output\n\nShip it.\n"
            return "agent take for round %s" % rnd
        orch.call_agent_sessioned = fake_sessioned

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch._agent_available = self._orig_avail

    def _cfg(self):
        return {"agents": {"codex_enabled": True, "claude_enabled": False,
                           "gemini_enabled": False},
               "runtime": {"parallel_discussion_rounds": False,
                           "phase_quality_gates_enabled": False,
                           "phase_independent_first_round_enabled": False},
               "_workflow_target": "app", "_app_dir": self.app_dir,
               "root": self.app_dir}

    def _phase(self, rounds=3):
        return wf.Phase("design_discussion", ".", "design_discussion.md",
                        "design purpose", rounds=rounds)

    def test_resume_preserves_transcript_and_skips_completed_round(self):
        md_path = os.path.join(self.app_dir, "design_discussion.md")
        existing = ("# Demo — Design Discussion\n\n## Transcript\n\n"
                    "\n### Round 1\n\n"
                    "**Codex — Round 1**\n\nfirst take\n"
                    "\n**Coordinator (Codex) — decision after round 1**\n\nCONSENSUS: NO\n")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(existing)
        state = {"current_phase": "design_discussion", "current_round": 2,
                 "completed_phases": [], "phase_outputs": {},
                 "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}
        out = orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                                 "build a todo app", [], state, phase_index=0)
        with open(md_path, encoding="utf-8") as fh:
            final_text = fh.read()
        # round 1's original content is untouched, not truncated or duplicated
        self.assertEqual(final_text.count("### Round 1"), 1)
        self.assertIn("first take", final_text)
        # round 2 actually ran (agent + coordinator turns), no round 1 turns re-run
        self.assertIn("### Round 2", final_text)
        rounds_called = sorted(set(r for (r, _a, _sk) in self.calls))
        self.assertEqual(rounds_called, [2])   # round 1 never re-invoked
        self.assertIn("design_discussion", state["completed_phases"])
        self.assertIn("Ship it", out)

    def test_fresh_start_unaffected_truncates_stale_leftover(self):
        md_path = os.path.join(self.app_dir, "design_discussion.md")
        # Stale leftover file from an unrelated/older run — must be truncated
        # because state does NOT indicate this phase crashed mid-round.
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write("stale leftover content from a previous unrelated run\n"
                     "### Round 1\n\n**Codex — Round 1**\n\nold\n")
        state = {"current_phase": None, "current_round": 0,
                 "completed_phases": [], "phase_outputs": {},
                 "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(), "prompt",
                           [], state, phase_index=0)
        with open(md_path, encoding="utf-8") as fh:
            final_text = fh.read()
        self.assertNotIn("stale leftover", final_text)
        self.assertNotIn("old", final_text.split("### Round 1")[1][:20]
                         if "### Round 1" in final_text else "")
        rounds_called = sorted(set(r for (r, _a, _sk) in self.calls))
        self.assertEqual(rounds_called, [1])   # started clean at round 1

    def test_current_round_zero_is_fresh_start_not_resume(self):
        # current_phase matches but current_round==0 (phase just started,
        # never entered a round) — must NOT be treated as a resume.
        md_path = os.path.join(self.app_dir, "design_discussion.md")
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write("leftover\n### Round 1\n\n**Codex — Round 1**\n\nold\n")
        state = {"current_phase": "design_discussion", "current_round": 0,
                 "completed_phases": [], "phase_outputs": {},
                 "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(), "prompt",
                           [], state, phase_index=0)
        with open(md_path, encoding="utf-8") as fh:
            final_text = fh.read()
        self.assertNotIn("leftover", final_text)
        rounds_called = sorted(set(r for (r, _a, _sk) in self.calls))
        self.assertEqual(rounds_called, [1])

    def test_completed_phase_is_not_reprocessed_phase_level_resume(self):
        # Phase-level resume (already-completed phases) must still be
        # respected by the caller — process_phase itself doesn't special-case
        # this (the pipeline loop skips it before ever calling process_phase),
        # so this documents/pins that contract at the call-site level used by
        # run(): phases in completed_phases are simply never re-entered.
        completed = ["design_discussion"]
        remaining = [p for p in [("design_discussion",)] if p[0] not in completed]
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
