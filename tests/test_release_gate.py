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

    def test_second_repair_still_clears_build_phases_when_prompt_unchanged(self):
        # Regression: a PRIOR repair already appended "## Change requested",
        # so the append guard skips it this time — the prompt hash never
        # changes. Without explicitly clearing completed_phases here, the
        # next pass would skip build_coordination/final_review (already
        # marked complete from the failed attempt) and burn the whole repair
        # budget doing nothing, per the exact failure seen live on longwave.
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
        orch._queue_release_gate_repair("x", self.dir, state, "compile failed",
                                        phases=phases, build_phase_key="build_coordination")
        self.assertEqual(state["completed_phases"], ["iterate_scope"])
        self.assertNotIn("build_coordination", state["phase_outputs"])
        self.assertNotIn("final_review", state["phase_outputs"])
        self.assertNotIn("build_coordination", state["consensus_status"])
        # iterate_scope (before the build phase) is untouched — no need to redo it.
        self.assertEqual(state["phase_outputs"]["iterate_scope"], "scope")
        # the append guard still holds — the prompt text itself is unchanged
        with open(os.path.join(self.dir, "initial_prompt",
                               "initial_prompt.md")) as fh:
            text = fh.read()
        self.assertEqual(text.count("## Change requested"), 1)


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
