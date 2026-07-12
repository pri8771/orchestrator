"""Tests for the docs backfill feature (backfill.py):

- gathers <app>/docs/*.md|*.txt and distills them into pending phases via one
  claude call per phase (call_agent is stubbed — no real CLI calls)
- writes <app>/<folder>/<file> with the backfill header + updates state
- stops the whole loop at NOT_COVERED / a raising call and deletes the marker
- refuses writes:true, verify, and qa/review/verification phases (build/QA
  always runs live)
- no marker or no docs => complete no-op
"""
import os
import shutil
import tempfile
import unittest

import backfill
import orchestrator as orch


def _phase(key, writes=False, verify=None, purpose="the purpose"):
    return {"key": key, "folder": key, "file": key + ".md",
            "title": key.replace("_", " ").title(), "purpose": purpose,
            "writes": writes, "verify": verify}


PHASES = [
    _phase("initial_discussion", purpose="understand the app"),
    _phase("tech_specs", purpose="decide the stack"),
    _phase("build_coordination", writes=True),
    _phase("final_review"),
]


class _FakeAgent:
    """Stub for orchestrator.call_agent(cfg, app, phase, rnd, agent, prompt)."""

    def __init__(self, replies=None, exc=None):
        self.replies = dict(replies or {})
        self.exc = exc
        self.calls = []

    def __call__(self, cfg, app, phase, rnd, agent, prompt):
        self.calls.append((phase, rnd, agent, prompt))
        if self.exc is not None:
            raise self.exc
        return self.replies.get(phase, "Distilled output for %s." % phase)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "docs"))
        with open(os.path.join(self.dir, "docs", "spec.md"), "w") as fh:
            fh.write("# My spec\n\nThe app is a tide tracker.")
        with open(os.path.join(self.dir, "docs", "notes.txt"), "w") as fh:
            fh.write("Extra notes about tides.")
        self._arm()
        self.state = {"completed_phases": [], "phase_outputs": {}}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _arm(self):
        open(os.path.join(self.dir, backfill.MARKER), "w").close()

    def _marker_exists(self):
        return os.path.exists(os.path.join(self.dir, backfill.MARKER))


class TestGatherDocs(_Base):
    def test_concatenates_md_and_txt(self):
        docs = backfill.gather_docs(self.dir, emit=lambda m: None)
        self.assertIn("tide tracker", docs)
        self.assertIn("Extra notes", docs)
        self.assertIn("spec.md", docs)

    def test_caps_total_chars(self):
        with open(os.path.join(self.dir, "docs", "huge.txt"), "w") as fh:
            fh.write("x" * (backfill.MAX_DOC_CHARS + 5000))
        warned = []
        docs = backfill.gather_docs(self.dir, emit=warned.append)
        self.assertLessEqual(len(docs), backfill.MAX_DOC_CHARS)
        self.assertTrue(any("truncated" in w for w in warned))

    def test_missing_docs_dir_is_empty(self):
        shutil.rmtree(os.path.join(self.dir, "docs"))
        self.assertEqual(backfill.gather_docs(self.dir, emit=lambda m: None), "")


class TestRunBackfill(_Base):
    def test_backfills_files_and_state_then_stops_at_build(self):
        fake = _FakeAgent()
        backfill.run_backfill({}, "tides", self.dir, PHASES, self.state, fake)
        # The two discussion phases before the build phase were backfilled...
        self.assertEqual([c[0] for c in fake.calls],
                         ["initial_discussion", "tech_specs"])
        self.assertEqual(self.state["completed_phases"],
                         ["initial_discussion", "tech_specs"])
        p = os.path.join(self.dir, "initial_discussion", "initial_discussion.md")
        with open(p) as fh:
            body = fh.read()
        self.assertIn("# tides — Initial Discussion", body)
        self.assertIn("_Backfilled from user-supplied docs._", body)
        self.assertIn("Distilled output for initial_discussion.", body)
        self.assertEqual(self.state["phase_outputs"]["tech_specs"],
                         "Distilled output for tech_specs.")
        # ...the writes:true phase stopped the loop (final_review never called,
        # and no build file was written)...
        self.assertNotIn("build_coordination", self.state["completed_phases"])
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "build_coordination")))
        # ...state was persisted and the marker consumed.
        self.assertEqual(orch.load_state(self.dir)["completed_phases"],
                         ["initial_discussion", "tech_specs"])
        self.assertFalse(self._marker_exists())

    def test_prompt_contains_docs_purpose_and_instructions(self):
        fake = _FakeAgent()
        backfill.run_backfill({}, "tides", self.dir, PHASES[:1], self.state, fake)
        prompt = fake.calls[0][3]
        self.assertIn("tide tracker", prompt)
        self.assertIn("understand the app", prompt)
        self.assertIn("NOT_COVERED", prompt)
        self.assertEqual(fake.calls[0][1:3], ("backfill", "claude"))

    def test_not_covered_stops_everything_and_deletes_marker(self):
        fake = _FakeAgent(replies={"initial_discussion": "  NOT_COVERED\n"})
        backfill.run_backfill({}, "tides", self.dir, PHASES, self.state, fake)
        self.assertEqual(len(fake.calls), 1)  # never reached tech_specs
        self.assertEqual(self.state["completed_phases"], [])
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "initial_discussion")))
        self.assertFalse(self._marker_exists())

    def test_agent_failure_stops_everything_and_deletes_marker(self):
        fake = _FakeAgent(exc=RuntimeError("CLI unavailable"))
        backfill.run_backfill({}, "tides", self.dir, PHASES, self.state, fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.state["completed_phases"], [])
        self.assertFalse(self._marker_exists())

    def test_empty_reply_does_not_complete_phase(self):
        # A blank reply must be treated like NOT_COVERED, not blessed as a doc.
        for empty in ("", "   ", "\n\t"):
            self._arm()
            state = {"completed_phases": [], "phase_outputs": {}}
            fake = _FakeAgent(replies={"initial_discussion": empty})
            backfill.run_backfill({}, "tides", self.dir, PHASES, state, fake)
            self.assertEqual(state["completed_phases"], [], repr(empty))
            self.assertFalse(os.path.exists(
                os.path.join(self.dir, "initial_discussion")), repr(empty))
            self.assertFalse(self._marker_exists())

    def test_requires_verification_and_reads_target_are_live_only(self):
        for extra in ({"requires_verification": True}, {"reads_target": True}):
            phase = _phase("some_phase")
            phase.update(extra)
            self._arm()
            state = {"completed_phases": [], "phase_outputs": {}}
            fake = _FakeAgent()
            backfill.run_backfill({}, "tides", self.dir, [phase], state, fake)
            self.assertEqual(fake.calls, [], str(extra))
            self.assertEqual(state["completed_phases"], [])

    def test_path_traversal_folder_is_refused(self):
        outside = os.path.join(os.path.dirname(self.dir), "escaped.md")
        phase = _phase("evil")
        phase["folder"] = "../.."
        phase["file"] = "escaped.md"
        fake = _FakeAgent()
        backfill.run_backfill({}, "tides", self.dir, [phase], self.state, fake)
        self.assertEqual(self.state["completed_phases"], [])
        self.assertFalse(os.path.exists(outside))
        self.assertFalse(self._marker_exists())

    def test_refuses_writes_verify_and_qa_phases_first(self):
        for phase in (_phase("build_coordination", writes=True),
                      _phase("build_verification",
                             verify={"type": "xcodebuild"}),
                      _phase("human_qa_checklist"),
                      _phase("final_review"),
                      _phase("build_verification")):
            fake = _FakeAgent()
            self._arm()
            state = {"completed_phases": [], "phase_outputs": {}}
            backfill.run_backfill({}, "tides", self.dir, [phase], state, fake)
            self.assertEqual(fake.calls, [], "called live-only %s" % phase["key"])
            self.assertEqual(state["completed_phases"], [])
            self.assertFalse(self._marker_exists())

    def test_already_completed_phases_skipped(self):
        self.state["completed_phases"] = ["initial_discussion"]
        fake = _FakeAgent()
        backfill.run_backfill({}, "tides", self.dir, PHASES, self.state, fake)
        self.assertEqual([c[0] for c in fake.calls], ["tech_specs"])

    def test_no_marker_is_a_noop(self):
        os.remove(os.path.join(self.dir, backfill.MARKER))
        fake = _FakeAgent()
        backfill.run_backfill({}, "tides", self.dir, PHASES, self.state, fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.state["completed_phases"], [])

    def test_marker_without_docs_is_a_noop(self):
        shutil.rmtree(os.path.join(self.dir, "docs"))
        fake = _FakeAgent()
        backfill.run_backfill({}, "tides", self.dir, PHASES, self.state, fake)
        self.assertEqual(fake.calls, [])
        self.assertTrue(self._marker_exists())  # nothing consumed it


if __name__ == "__main__":
    unittest.main()
