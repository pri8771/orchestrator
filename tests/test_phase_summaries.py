"""Phase-transition summarization (V2 deep-research recommendation #1):
phase_summaries.json generation/parsing/merge, the hybrid recency-window
build_context injection (recent phases raw, older phases summarized), the
runtime.phase_summary_policy config knob's both branches, and a sanity check
that the hybrid policy's prompt is meaningfully smaller than legacy's for a
multi-phase run."""
import json
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


def _block(payload):
    return "```phase-summary-json\n%s\n```" % json.dumps(payload)


class TestParsePhaseSummaryBlocks(unittest.TestCase):
    def test_parses_valid_block(self):
        summary, errors = orch.parse_phase_summary_blocks(
            _block({"phase": "design", "one_paragraph_summary": "Chose SwiftUI.",
                    "key_decisions": ["D-1"], "open_risks": ["perf on old devices"]}))
        self.assertEqual(errors, [])
        self.assertEqual(summary["phase"], "design")
        self.assertEqual(summary["key_decisions"], ["D-1"])

    def test_missing_required_field_reported_not_silently_dropped(self):
        summary, errors = orch.parse_phase_summary_blocks(
            _block({"phase": "design"}))  # no one_paragraph_summary
        self.assertIsNone(summary)
        self.assertEqual(len(errors), 1)

    def test_last_emission_wins(self):
        text = (_block({"phase": "x", "one_paragraph_summary": "draft"})
                + "\n" + _block({"phase": "x", "one_paragraph_summary": "final"}))
        summary, _ = orch.parse_phase_summary_blocks(text)
        self.assertEqual(summary["one_paragraph_summary"], "final")

    def test_no_block_returns_none(self):
        summary, errors = orch.parse_phase_summary_blocks("just prose, no fence")
        self.assertIsNone(summary)
        self.assertEqual(errors, [])


class TestPersistPhaseSummaries(unittest.TestCase):
    def test_merge_appends_and_replaces_by_phase(self):
        with tempfile.TemporaryDirectory() as d:
            orch.merge_phase_summary(d, "design", {"phase": "design",
                                                    "one_paragraph_summary": "v1"})
            orch.merge_phase_summary(d, "specs", {"phase": "specs",
                                                   "one_paragraph_summary": "s1"})
            merged = orch.merge_phase_summary(d, "design", {"phase": "design",
                                                             "one_paragraph_summary": "v2"})
            self.assertEqual([m["phase"] for m in merged], ["design", "specs"])
            self.assertEqual(merged[0]["one_paragraph_summary"], "v2")
            # persisted atomically and loadable
            self.assertEqual(orch.load_phase_summaries(d), merged)
            with open(os.path.join(d, "phase_summaries.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("schema_version", data)

    def test_phase_summary_for_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            orch.merge_phase_summary(d, "design", {"phase": "design",
                                                    "one_paragraph_summary": "v1"})
            self.assertEqual(orch.phase_summary_for(d, "design")["one_paragraph_summary"], "v1")
            self.assertIsNone(orch.phase_summary_for(d, "nope"))

    def test_no_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(orch.load_phase_summaries(d), [])


class TestRenderPhaseSummary(unittest.TestCase):
    def test_renders_paragraph_and_lists(self):
        text = orch.render_phase_summary({
            "phase": "design", "one_paragraph_summary": "Chose SwiftUI.",
            "key_decisions": ["D-1", "D-2"], "open_risks": ["perf"]})
        self.assertIn("Chose SwiftUI.", text)
        self.assertIn("key decisions: D-1, D-2", text)
        self.assertIn("open risks: perf", text)

    def test_empty_summary_renders_blank(self):
        self.assertEqual(orch.render_phase_summary({}), "")
        self.assertEqual(orch.render_phase_summary(None), "")


class TestRecordPhaseContractsGeneratesSummary(unittest.TestCase):
    def test_compliant_block_persisted_with_key_forced_to_phase(self):
        with tempfile.TemporaryDirectory() as app_dir:
            blob = _block({"phase": "WRONG_KEY", "one_paragraph_summary": "did X"})
            orch._record_phase_contracts({}, "demo", app_dir, "design_discussion",
                                         blob, "", record_decisions=False)
            summ = orch.phase_summary_for(app_dir, "design_discussion")
            self.assertIsNotNone(summ)
            self.assertEqual(summ["phase"], "design_discussion")  # never trust agent's key
            self.assertEqual(summ["one_paragraph_summary"], "did X")

    def test_no_compliant_block_falls_back_to_final_output(self):
        with tempfile.TemporaryDirectory() as app_dir:
            orch._record_phase_contracts({}, "demo", app_dir, "build_coordination",
                                         "some transcript, no fenced block",
                                         "The app now supports offline mode.",
                                         record_decisions=False)
            summ = orch.phase_summary_for(app_dir, "build_coordination")
            self.assertIsNotNone(summ)
            self.assertIn("offline mode", summ["one_paragraph_summary"])
            self.assertEqual(summ["key_decisions"], [])

    def test_every_phase_gets_a_summary_not_just_decision_phases(self):
        # record_decisions=False (build/verify/audit phases) must still get a
        # phase_summaries.json entry — the hybrid build_context policy needs
        # one for every completed phase, not just decision-bearing ones.
        with tempfile.TemporaryDirectory() as app_dir:
            orch._record_phase_contracts({}, "demo", app_dir, "verify_repair",
                                         "", "fixed the crash", record_decisions=False)
            self.assertIsNotNone(orch.phase_summary_for(app_dir, "verify_repair"))


class TestContractRequestIncludesSummary(unittest.TestCase):
    def test_every_phase_contract_requests_phase_summary(self):
        cfg = {"_workflow_target": "app"}
        phase = wf.Phase("design_discussion", ".", "d.md", "design")
        self.assertIn("phase-summary-json", orch._phase_contract(cfg, phase))
        build_phase = wf.Phase("build_coordination", ".", "b.md", "b", writes=True)
        self.assertIn("phase-summary-json", orch._phase_contract(cfg, build_phase))


class TestHybridPriorContext(unittest.TestCase):
    def _phases_with_transcripts(self, app_dir, keys_and_texts):
        phases = []
        for key, text in keys_and_texts:
            folder = key
            os.makedirs(os.path.join(app_dir, folder), exist_ok=True)
            with open(os.path.join(app_dir, folder, key + ".md"), "w",
                     encoding="utf-8") as fh:
                fh.write(text)
            phases.append((key, folder, key + ".md", "purpose of " + key))
        return phases

    def test_recent_phases_raw_older_phases_summarized(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._phases_with_transcripts(app_dir, [
                ("p1", "RAW TRANSCRIPT ONE " * 50),
                ("p2", "RAW TRANSCRIPT TWO " * 50),
                ("p3", "RAW TRANSCRIPT THREE " * 50),
            ])
            orch.merge_phase_summary(app_dir, "p1", {
                "phase": "p1", "one_paragraph_summary": "P1 SUMMARY TEXT"})
            orch.merge_phase_summary(app_dir, "p2", {
                "phase": "p2", "one_paragraph_summary": "P2 SUMMARY TEXT"})
            ctx = orch.hybrid_prior_context(app_dir, phases, ["p1", "p2", "p3"],
                                            recency_window=1)
            # only p3 (most recent) is raw; p1/p2 are summarized
            self.assertNotIn("RAW TRANSCRIPT ONE", ctx)
            self.assertNotIn("RAW TRANSCRIPT TWO", ctx)
            self.assertIn("RAW TRANSCRIPT THREE", ctx)
            self.assertIn("P1 SUMMARY TEXT", ctx)
            self.assertIn("P2 SUMMARY TEXT", ctx)

    def test_recency_window_two_keeps_last_two_raw(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._phases_with_transcripts(app_dir, [
                ("p1", "RAW ONE " * 50), ("p2", "RAW TWO " * 50),
                ("p3", "RAW THREE " * 50)])
            orch.merge_phase_summary(app_dir, "p1", {
                "phase": "p1", "one_paragraph_summary": "P1 SUMMARY"})
            ctx = orch.hybrid_prior_context(app_dir, phases, ["p1", "p2", "p3"],
                                            recency_window=2)
            self.assertNotIn("RAW ONE", ctx)
            self.assertIn("RAW TWO", ctx)
            self.assertIn("RAW THREE", ctx)
            self.assertIn("P1 SUMMARY", ctx)

    def test_missing_summary_falls_back_to_raw_never_silently_drops(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._phases_with_transcripts(
                app_dir, [("p1", "RAW UNSUMMARIZED CONTENT"), ("p2", "RAW TWO")])
            # no summary ever persisted for p1
            ctx = orch.hybrid_prior_context(app_dir, phases, ["p1", "p2"],
                                            recency_window=1)
            self.assertIn("RAW UNSUMMARIZED CONTENT", ctx)
            self.assertIn("no summary available", ctx)

    def test_incomplete_phase_not_included(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._phases_with_transcripts(app_dir, [("p1", "RAW ONE")])
            ctx = orch.hybrid_prior_context(app_dir, phases, [], recency_window=2)
            self.assertEqual(ctx, "")


class TestSelectPriorContextPolicyKnob(unittest.TestCase):
    def _setup(self, app_dir):
        phases = []
        for i in range(1, 6):
            key = "phase%d" % i
            os.makedirs(os.path.join(app_dir, key), exist_ok=True)
            with open(os.path.join(app_dir, key, key + ".md"), "w",
                     encoding="utf-8") as fh:
                fh.write(("full raw debate transcript for %s " % key) * 200)
            phases.append((key, key, key + ".md", "purpose"))
            orch.merge_phase_summary(app_dir, key, {
                "phase": key, "one_paragraph_summary": "%s decided X." % key})
        return phases

    def test_legacy_policy_returns_full_raw(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._setup(app_dir)
            cfg = {"runtime": {"phase_summary_policy": "legacy"}}
            upcoming = wf.Phase("phase6", ".", "p6.md", "purpose")
            ctx = orch._select_prior_context(cfg, app_dir, phases,
                                             [p[0] for p in phases], upcoming)
            for i in range(1, 6):
                self.assertIn("full raw debate transcript for phase%d" % i, ctx)

    def test_hybrid_policy_is_default_and_shrinks_prompt(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._setup(app_dir)
            legacy_cfg = {"runtime": {"phase_summary_policy": "legacy"}}
            hybrid_cfg = {"runtime": {}}   # default is hybrid
            upcoming = wf.Phase("phase6", ".", "p6.md", "purpose")
            completed = [p[0] for p in phases]
            legacy_ctx = orch._select_prior_context(legacy_cfg, app_dir, phases,
                                                     completed, upcoming)
            hybrid_ctx = orch._select_prior_context(hybrid_cfg, app_dir, phases,
                                                     completed, upcoming)
            self.assertLess(len(hybrid_ctx), len(legacy_ctx))
            # meaningfully smaller, not just marginally
            self.assertLess(len(hybrid_ctx), len(legacy_ctx) * 0.5)

    def test_build_phase_uses_recency_window_one(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._setup(app_dir)
            cfg = {"runtime": {}}
            upcoming = wf.Phase("build_coordination", ".", "b.md", "b", writes=True)
            completed = [p[0] for p in phases]
            ctx = orch._select_prior_context(cfg, app_dir, phases, completed, upcoming)
            # only the single most recent phase (phase5) stays raw
            self.assertIn("full raw debate transcript for phase5", ctx)
            self.assertNotIn("full raw debate transcript for phase4", ctx)

    def test_discussion_phase_uses_recency_window_two(self):
        with tempfile.TemporaryDirectory() as app_dir:
            phases = self._setup(app_dir)
            cfg = {"runtime": {}}
            upcoming = wf.Phase("phase6", ".", "p6.md", "purpose")
            completed = [p[0] for p in phases]
            ctx = orch._select_prior_context(cfg, app_dir, phases, completed, upcoming)
            self.assertIn("full raw debate transcript for phase5", ctx)
            self.assertIn("full raw debate transcript for phase4", ctx)
            self.assertNotIn("full raw debate transcript for phase3", ctx)


class TestBudgetSizeSanityCheck(unittest.TestCase):
    def test_default_policy_prompt_smaller_for_five_plus_phase_run(self):
        """End-to-end sanity check through build_context itself (not just the
        raw context string), for a 5+ completed-phase fixture."""
        with tempfile.TemporaryDirectory() as app_dir:
            phases = []
            for i in range(1, 7):
                key = "phase%d" % i
                os.makedirs(os.path.join(app_dir, key), exist_ok=True)
                with open(os.path.join(app_dir, key, key + ".md"), "w",
                         encoding="utf-8") as fh:
                    fh.write(("debate content for %s. " % key) * 300)
                phases.append((key, key, key + ".md", "purpose"))
                orch.merge_phase_summary(app_dir, key, {
                    "phase": key, "one_paragraph_summary": "%s: decided X." % key})
            completed = [p[0] for p in phases]

            legacy_disc = orch.prior_discussion_context(app_dir, phases, completed)
            hybrid_disc = orch.hybrid_prior_context(app_dir, phases, completed,
                                                     recency_window=2)

            def _ctx(disc_text):
                cfg = {"_app_dir": app_dir, "runtime": {},
                       "_prior_discussions": disc_text}
                return orch.build_context(cfg, "demo", ("phase7", "f", "f.md", "p"),
                                          "original prompt", [], "")

            legacy_ctx = _ctx(legacy_disc)
            hybrid_ctx = _ctx(hybrid_disc)
            self.assertLess(len(hybrid_ctx), len(legacy_ctx))


if __name__ == "__main__":
    unittest.main()
