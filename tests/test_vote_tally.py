"""Forced-vote ballots: vote-json parsing, the deterministic Python tally,
and the end-to-end forced-vote path through process_phase (previously the
largest untested engine branch)."""
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


class TestNormalizeVoteChoice(unittest.TestCase):
    VOTERS = ["codex", "claude", "local:glm4:9b"]

    def test_display_name_and_id_both_match(self):
        self.assertEqual(orch._normalize_vote_choice("Codex", self.VOTERS), "codex")
        self.assertEqual(orch._normalize_vote_choice("claude", self.VOTERS), "claude")
        self.assertEqual(orch._normalize_vote_choice('"Claude"', self.VOTERS), "claude")
        self.assertEqual(
            orch._normalize_vote_choice("local:glm4:9b", self.VOTERS), "local:glm4:9b")

    def test_unknown_and_empty_are_none(self):
        self.assertIsNone(orch._normalize_vote_choice("GPT-9", self.VOTERS))
        self.assertIsNone(orch._normalize_vote_choice("", self.VOTERS))
        self.assertIsNone(orch._normalize_vote_choice(None, self.VOTERS))


def _ballot(choice, confidence, reason="because"):
    return ('I back %s.\n```vote-json\n{"choice": "%s", "confidence": %s, '
            '"reason": "%s"}\n```\n' % (choice, choice, confidence, reason))


class TestParseVoteBallots(unittest.TestCase):
    VOTERS = ["codex", "claude", "gemini"]

    def test_valid_ballots_parse(self):
        errors = []
        ballots = orch.parse_vote_ballots(
            {"codex": _ballot("Claude", 4), "claude": _ballot("Gemini", 2)},
            self.VOTERS, on_error=errors.append)
        self.assertEqual(errors, [])
        self.assertEqual(
            [(b["voter"], b["choice"], b["confidence"]) for b in ballots],
            [("codex", "claude", 4), ("claude", "gemini", 2)])

    def test_self_vote_is_invalid(self):
        errors = []
        ballots = orch.parse_vote_ballots(
            {"codex": _ballot("Codex", 5)}, self.VOTERS, on_error=errors.append)
        self.assertEqual(ballots, [])
        self.assertTrue(any("self-vote" in e for e in errors))

    def test_unknown_choice_and_bad_confidence_are_invalid(self):
        errors = []
        ballots = orch.parse_vote_ballots(
            {"codex": _ballot("GPT-9", 3),
             "claude": ('```vote-json\n{"choice": "Codex", "confidence": "lots"}\n```')},
            self.VOTERS, on_error=errors.append)
        self.assertEqual(ballots, [])
        self.assertEqual(len(errors), 2)

    def test_missing_block_reported(self):
        errors = []
        ballots = orch.parse_vote_ballots(
            {"codex": "I just really like Claude's idea, trust me."},
            self.VOTERS, on_error=errors.append)
        self.assertEqual(ballots, [])
        self.assertTrue(any("no parseable" in e for e in errors))

    def test_last_block_wins_and_confidence_clamped(self):
        text = _ballot("Gemini", 3) + _ballot("Claude", 99)
        ballots = orch.parse_vote_ballots({"codex": text}, self.VOTERS)
        self.assertEqual(len(ballots), 1)
        self.assertEqual(ballots[0]["choice"], "claude")
        self.assertEqual(ballots[0]["confidence"], 5)


class TestTallyVotes(unittest.TestCase):
    VOTERS = ["codex", "claude", "gemini"]

    def _b(self, voter, choice, conf):
        return {"voter": voter, "choice": choice, "confidence": conf}

    def test_confidence_weighted_winner(self):
        winner, weights = orch.tally_votes(
            [self._b("codex", "claude", 2), self._b("claude", "gemini", 5),
             self._b("gemini", "claude", 2)], self.VOTERS)
        self.assertEqual(weights, {"claude": 4, "gemini": 5})
        self.assertEqual(winner, "gemini")

    def test_tie_breaks_by_coordinator_preference(self):
        winner, weights = orch.tally_votes(
            [self._b("gemini", "codex", 3), self._b("codex", "claude", 3)],
            self.VOTERS)
        self.assertEqual(weights, {"codex": 3, "claude": 3})
        self.assertEqual(winner, "claude")   # COORDINATOR_PREFERENCE order

    def test_empty_ballots(self):
        winner, weights = orch.tally_votes([], self.VOTERS)
        self.assertIsNone(winner)
        self.assertEqual(weights, {})


class TestFinalRoundPromptIsHonest(unittest.TestCase):
    """Regression: the final round must not force CONSENSUS: YES regardless of
    real disagreement. That's what made the forced-vote path above
    unreachable in practice — the coordinator always self-reported agreement
    on the last round, so `process_phase` never saw `consensus=False` at the
    point the vote check runs."""

    def _phase(self):
        return wf.Phase("app_features", "app_features", "app_features.md",
                        "Prioritize features.", rounds=1)

    def test_final_round_permits_an_honest_no(self):
        cfg = {"_workflow_target": "app"}
        prompt = orch.prompt_coordinate(cfg, "claude", "ctx", self._phase(), 1,
                                        final_round=True)
        self.assertNotIn("even if it wasn't unanimous", prompt)
        self.assertIn("CONSENSUS: NO", prompt)
        self.assertIn("weighted vote", prompt)

    def test_non_final_round_still_asks_for_an_honest_read(self):
        cfg = {"_workflow_target": "app"}
        prompt = orch.prompt_coordinate(cfg, "claude", "ctx", self._phase(), 1,
                                        final_round=False)
        self.assertIn("Don't invent agreement", prompt)


class TestForcedVoteEndToEnd(unittest.TestCase):
    """Drive process_phase to the forced vote with fake agents."""

    def setUp(self):
        self._call_agent = orch.call_agent
        self._avail = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True

    def tearDown(self):
        orch.call_agent = self._call_agent
        orch._agent_available = self._avail

    def _cfg(self):
        return {
            "agents": {"codex_enabled": True, "claude_enabled": True,
                       "gemini_enabled": False, "ollama_enabled": False},
            "runtime": {},
            "_workflow_target": "app",
        }

    def _run(self, vote_replies, tally_reply=None):
        app_dir = tempfile.mkdtemp()
        phase = wf.Phase("app_features", "app_features", "app_features.md",
                         "Prioritize features.", rounds=1)
        prompts = []

        def fake_call_agent(_cfg, _app, _phase, rnd, agent, prompt):
            prompts.append((str(rnd), agent, prompt))
            if "TIME TO DECIDE" in prompt:
                return vote_replies[agent]
            if "calls it" in prompt:
                return tally_reply or "no idea"
            if "YOUR TURN" in prompt:
                return "%s_PROPOSAL_TEXT" % agent.upper()
            return "CONSENSUS: NO\nStill split."   # coordinator wrap-up

        orch.call_agent = fake_call_agent
        out = orch.process_phase(self._cfg(), "demo", app_dir, phase,
                                 "Build a useful iOS app.", [],
                                 orch.load_state(app_dir), phase_index=0)
        return out, prompts

    def test_deterministic_tally_skips_llm_tally(self):
        out, prompts = self._run({
            "codex": _ballot("Claude", 4),
            "claude": _ballot("Codex", 2),
        })
        self.assertIn("VOTE_DECISION: YES", out)
        # claude wins 4-2; the winner's last substantive post is adopted.
        self.assertIn("CLAUDE_PROPOSAL_TEXT", out)
        self.assertIn("commits to Claude's proposal", out)
        # No LLM tally turn ran — the tally was counted in Python.
        self.assertFalse(any("calls it" in p for (_r, _a, p) in prompts))

    def test_undecided_vote_marks_phase_resolutions(self):
        # NEXT_MILESTONES "UNRESOLVED phase state": malformed ballots force
        # the LLM-tally fallback, and if THAT never actually decides either
        # (no VOTE_DECISION: YES), the phase closes anyway but must record
        # that it isn't a clean resolution.
        app_dir = tempfile.mkdtemp()
        phase = wf.Phase("app_features", "app_features", "app_features.md",
                         "Prioritize features.", rounds=1)

        def fake_call_agent(_cfg, _app, _phase, rnd, agent, prompt):
            if "TIME TO DECIDE" in prompt:
                return "I like Claude's plan best, no ballot though."
            if "calls it" in prompt:
                return "Still torn, no clear winner."   # no VOTE_DECISION: YES
            if "YOUR TURN" in prompt:
                return "%s_PROPOSAL_TEXT" % agent.upper()
            return "CONSENSUS: NO\nStill split."

        orch.call_agent = fake_call_agent
        orch.process_phase(self._cfg(), "demo", app_dir, phase,
                           "Build a useful iOS app.", [],
                           orch.load_state(app_dir), phase_index=0)
        state = orch.load_state(app_dir)
        self.assertFalse(state["vote_results"]["app_features"]["decided"])
        self.assertEqual(state["phase_resolutions"]["app_features"], "vote_undecided")
        with open(os.path.join(app_dir, "mistakes.jsonl"), encoding="utf-8") as fh:
            self.assertIn("vote_undecided", fh.read())

    def test_malformed_ballots_fall_back_to_llm_tally(self):
        out, prompts = self._run(
            {"codex": "I like Claude's plan best, no ballot though.",
             "claude": "Codex had the stronger idea."},
            tally_reply="The room favors Codex.\n## Final Output\nCodex's "
                        "plan.\nVOTE_DECISION: YES")
        self.assertIn("VOTE_DECISION: YES", out)
        self.assertTrue(any("calls it" in p for (_r, _a, p) in prompts))

    def test_votes_are_cast_by_every_available_agent(self):
        _out, prompts = self._run({
            "codex": _ballot("Claude", 3),
            "claude": _ballot("Codex", 3),
        })
        voters = {a for (_r, a, p) in prompts if "TIME TO DECIDE" in p}
        self.assertEqual(voters, {"codex", "claude"})


if __name__ == "__main__":
    unittest.main()
