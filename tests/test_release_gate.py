"""Regression tests for the exp7 post-mortem fixes:

- provider limit/auth banners are rejected as agent output, not recorded
- an app whose build fails verification is never marked done (release gate)
- the gate queues a capped iterate repair instead of looping forever
- a lost portfolio manifest is recovered from the phase transcript first
"""
import json
import os
import shutil
import tempfile
import unittest

import orchestrator as orch
import verify as verifylib
import workflows as wf


class TestProviderBanner(unittest.TestCase):
    def test_spend_limit_banner_detected(self):
        self.assertTrue(orch._provider_banner(
            "You've hit your monthly spend limit · raise it at "
            "claude.ai/settings/usage"))

    def test_login_banner_detected(self):
        self.assertTrue(orch._provider_banner("Not logged in. Please run /login"))

    def test_long_answer_quoting_banner_passes(self):
        text = ("The research shows retention drops when users hit your usage "
                "limit messaging too early. " * 20)
        self.assertIsNone(orch._provider_banner(text))

    def test_normal_short_answer_passes(self):
        self.assertIsNone(orch._provider_banner("CONSENSUS: YES — ship it."))


class _GateBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "initial_prompt"))
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("Build a thing.")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _pbxproj(self):
        p = os.path.join(self.dir, "app_build", "X.xcodeproj")
        os.makedirs(p)
        open(os.path.join(p, "project.pbxproj"), "w").close()

    def _verify_record(self, ok):
        verifylib.persist_verify_result(
            self.dir, "build_verification",
            {"ran": True, "ok": ok, "status": "ok" if ok else "failed",
             "summary": "test", "tool": "xcodebuild"}, attempt=0)


VERIFY_PHASES = [{"key": "build_verification", "verify": {"type": "xcodebuild"}}]


class TestReleaseGate(_GateBase):
    def test_no_verify_phases_exempt(self):
        self.assertIsNone(orch._release_gate_failure(
            self.dir, [{"key": "brainstorm"}], {}, "Build a thing."))

    def test_unverified_no_toolchain_passes_gate(self):
        # No verify record at all (e.g. no Xcode on this machine): best-effort
        # stance — never block on what we couldn't check.
        self.assertIsNone(orch._release_gate_failure(
            self.dir, VERIFY_PHASES, {}, "Build a thing."))

    def test_failed_verification_fails_gate(self):
        self._pbxproj()
        self._verify_record(ok=False)
        self.assertIn("verification failed", orch._release_gate_failure(
            self.dir, VERIFY_PHASES, {}, "Build a thing."))

    def test_green_build_passes_gate(self):
        self._pbxproj()
        self._verify_record(ok=True)
        self.assertIsNone(orch._release_gate_failure(
            self.dir, VERIFY_PHASES, {}, "Build a thing."))


class TestQueueReleaseGateRepair(_GateBase):
    def test_queues_iterate_repair_and_never_marks_done(self):
        state = {}
        orch._queue_release_gate_repair("x", self.dir, state, "compile failed")
        self.assertFalse(state["done"])
        self.assertIn("release gate", state["error"])
        self.assertEqual(state["release_gate_repairs"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.dir, ".repair_pending")))
        with open(os.path.join(self.dir, "workflow.txt")) as fh:
            self.assertEqual(fh.read().strip(), "iterate")
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md")) as fh:
            self.assertIn("## Change requested", fh.read())

    def test_repair_budget_caps(self):
        state = {"release_gate_repairs": 2}
        orch._queue_release_gate_repair("x", self.dir, state, "compile failed")
        self.assertFalse(state["done"])
        self.assertEqual(state["release_gate_repairs"], 2)  # not incremented
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".repair_pending")))

    def test_second_repair_clears_build_phases_and_refreshes_the_ask(self):
        # A PRIOR repair already appended "## Change requested". This pass must
        # (a) clear completed_phases from the build phase on, or the next pass
        # skips build_coordination/final_review (marked done from the failed
        # attempt) and burns the budget doing nothing (seen live on longwave);
        # and (b) REPLACE the stale ask with THIS failure — a repair routinely
        # surfaces a different gate than the one that triggered it (fixing
        # UI-crawl reveals a design-lint letterbox error), and leaving the old
        # ask told the agents to fix an already-addressed problem (seen live
        # on steep: a launch-screen error never converged behind a stale
        # UI-crawl ask).
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md"), "a") as fh:
            fh.write("\n\n## Change requested\nAn earlier, unrelated ask.\n")
        phases = [
            wf.Phase("iterate_scope", "iterate_scope", "iterate_scope.md", "p"),
            wf.Phase("build_coordination", "build_coordination",
                     "agent_messages.md", "p", writes=True),
            wf.Phase("final_review", "final_review", "final_review.md", "p"),
        ]
        state = {
            "completed_phases": ["iterate_scope", "build_coordination", "final_review"],
            "phase_outputs": {"iterate_scope": "scope", "build_coordination": "built",
                              "final_review": "VERIFICATION: FAILED"},
            "consensus_status": {"build_coordination": True, "final_review": True},
        }
        orch._queue_release_gate_repair("x", self.dir, state,
                                        "missing_launch_screen at Info.plist",
                                        phases=phases, build_phase_key="build_coordination")
        self.assertEqual(state["completed_phases"], ["iterate_scope"])
        self.assertNotIn("build_coordination", state["phase_outputs"])
        self.assertNotIn("final_review", state["phase_outputs"])
        self.assertNotIn("build_coordination", state["consensus_status"])
        # iterate_scope (before the build phase) is untouched — no need to redo it.
        self.assertEqual(state["phase_outputs"]["iterate_scope"], "scope")
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md")) as fh:
            text = fh.read()
        # Still exactly one block (replaced, not stacked), now naming THIS
        # failure — the stale ask is gone so the agents fix the right thing.
        self.assertEqual(text.count("## Change requested"), 1)
        self.assertIn("missing_launch_screen", text)
        self.assertNotIn("An earlier, unrelated ask", text)

    def test_repair_preserves_original_prompt_body(self):
        # Replacing the ask must not eat the user's actual prompt.
        prompt_p = os.path.join(self.dir, "initial_prompt", "initial_prompt.md")
        with open(prompt_p) as fh:
            body = fh.read()
        orch._queue_release_gate_repair("x", self.dir, {}, "first failure")
        orch._queue_release_gate_repair("x", self.dir, {}, "second failure")
        with open(prompt_p) as fh:
            text = fh.read()
        self.assertTrue(text.startswith(body.rstrip("\n")))
        self.assertEqual(text.count("## Change requested"), 1)
        self.assertIn("second failure", text)
        self.assertNotIn("first failure", text)


class TestRepairBudgetSurvivesPromptHashReset(_GateBase):
    """The repair rewrite embeds dynamic failure text in the prompt tail, so
    every pass moves the prompt hash and takes the new-prompt reset — keyed
    on phash alone the reset zeroed release_gate_repairs before the cap was
    ever read, making the "capped" loop unbounded (the budget_exhausted
    artifact was dead code). A body-identical reset must carry the count;
    a real human edit to the body must still zero it."""

    def _hashes(self):
        # Mirrors _run_app_pipeline's computation for a target-less workflow.
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md")) as fh:
            prompt = fh.read()
        phash = orch.sha256_text(prompt + "\n#target:" + "\n#tsig:"
                                 + orch.sha256_text(""))
        bhash = orch.sha256_text(orch._prompt_body(prompt) + "\n#target:"
                                 + "\n#tsig:" + orch.sha256_text(""))
        return phash, bhash

    def test_budget_accumulates_across_tail_rewrites(self):
        state = {}
        phash, bhash = self._hashes()
        orch._reset_for_prompt_change(state, phash, bhash)
        self.assertEqual(state["release_gate_repairs"], 0)

        orch._queue_release_gate_repair("x", self.dir, state, "reason A")
        self.assertEqual(state["release_gate_repairs"], 1)
        phash2, bhash2 = self._hashes()
        self.assertNotEqual(phash, phash2)   # the rewrite moved the hash...
        self.assertEqual(bhash, bhash2)      # ...but not the body hash
        orch._reset_for_prompt_change(state, phash2, bhash2)
        self.assertEqual(state["release_gate_repairs"], 1)   # carried

        orch._queue_release_gate_repair("x", self.dir, state, "reason B")
        self.assertEqual(state["release_gate_repairs"], 2)
        phash3, bhash3 = self._hashes()
        orch._reset_for_prompt_change(state, phash3, bhash3)
        self.assertEqual(state["release_gate_repairs"], 2)   # still carried

        # The cap finally engages — a third repair is refused.
        orch._queue_release_gate_repair("x", self.dir, state, "reason C")
        self.assertEqual(state["release_gate_repairs"], 2)

    def test_real_body_edit_still_zeroes_the_budget(self):
        state = {}
        phash, bhash = self._hashes()
        orch._reset_for_prompt_change(state, phash, bhash)
        orch._queue_release_gate_repair("x", self.dir, state, "reason A")
        # The human rewrites the actual ask — a genuinely new build.
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("Build a DIFFERENT thing.")
        phash2, bhash2 = self._hashes()
        self.assertNotEqual(bhash, bhash2)
        orch._reset_for_prompt_change(state, phash2, bhash2)
        self.assertEqual(state["release_gate_repairs"], 0)


class TestManifestTranscriptRecovery(_GateBase):
    def test_recovers_fence_from_transcript_without_llm(self):
        os.makedirs(os.path.join(self.dir, "portfolio_selection"))
        manifest = {"apps": [{"name": "Nickel", "slug": "nickel", "build": True}]}
        with open(os.path.join(self.dir, "portfolio_selection",
                               "portfolio_selection.md"), "w") as fh:
            fh.write("### Round 1\n\n```portfolio-json\n%s\n```\n"
                     % json.dumps(manifest))
        state = {"phase_outputs": {"portfolio_selection":
                                   "You've hit your monthly spend limit"}}
        # cfg={} would crash on any LLM path — success proves no LLM was needed.
        self.assertTrue(orch._repair_portfolio_manifest({}, "x", self.dir, state))
        self.assertIn("nickel", state["phase_outputs"]["portfolio_selection"])


if __name__ == "__main__":
    unittest.main()
