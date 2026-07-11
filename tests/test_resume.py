"""--resume <project_slug> (V2 spec §27): resume an existing project from saved
state. Covers the prepare_resume contract: missing project / missing state
refuse; done-without-error is a no-op; an aborted project's error is cleared so
the pipeline re-enters; the CLI exposes the flag."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPrepareResume(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _mkproject(self, slug, state=None):
        app_dir = os.path.join(self.root, slug)
        os.makedirs(app_dir, exist_ok=True)
        if state is not None:
            with open(os.path.join(app_dir, "agent_state.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(state, fh)
        return app_dir

    def test_missing_project_refuses(self):
        rc, target = orch.prepare_resume(self.root, "nope")
        self.assertEqual(rc, 2)
        self.assertIsNone(target)

    def test_project_without_state_refuses(self):
        self._mkproject("fresh")
        rc, target = orch.prepare_resume(self.root, "fresh")
        self.assertEqual(rc, 2)
        self.assertIsNone(target)

    def test_done_project_is_noop(self):
        self._mkproject("done", {"done": True, "error": None,
                                 "completed_phases": ["a"]})
        rc, target = orch.prepare_resume(self.root, "done")
        self.assertEqual(rc, 0)
        self.assertIsNone(target)

    def test_incomplete_project_resumes(self):
        self._mkproject("partial", {"done": False, "error": None,
                                    "completed_phases": ["initial_discussion"]})
        rc, target = orch.prepare_resume(self.root, "partial")
        self.assertEqual(rc, 0)
        self.assertEqual(target, "partial")

    def test_aborted_project_error_cleared(self):
        app_dir = self._mkproject(
            "aborted", {"done": False, "error": "agent exploded",
                        "completed_phases": ["initial_discussion"]})
        rc, target = orch.prepare_resume(self.root, "aborted")
        self.assertEqual(rc, 0)
        self.assertEqual(target, "aborted")
        with open(os.path.join(app_dir, "agent_state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertIsNone(st["error"])
        self.assertFalse(st["done"])

    def test_stale_running_project_is_recovered(self):
        stale = {
            "status": "running",
            "done": False,
            "error": None,
            "current_phase": "app_features",
            "current_round": 2,
            "next_agent": "codex",
            "runner_pid": 999999,
            "completed_phases": ["product_research", "initial_discussion"],
            "phase_outputs": {},
            "consensus_status": {},
            "last_processed": "2020-01-01 00:00:00",
        }
        app_dir = self._mkproject("stale", stale)
        rc, target = orch.prepare_resume(self.root, "stale")
        self.assertEqual(rc, 0)
        self.assertEqual(target, "stale")
        with open(os.path.join(app_dir, "agent_state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertIsNone(st["error"])
        self.assertIsNone(st.get("blocked_conflict"))
        self.assertIsNone(st["awaiting_approval"] if "awaiting_approval" in st else None)


    def test_cli_exposes_resume_flag(self):
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"), "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("--resume", out.stdout)


if __name__ == "__main__":
    unittest.main()
