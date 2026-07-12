"""--continue-with <workflow> (staged continuation): re-open an existing
project under a different workflow, carrying prior phase outputs forward as
context. Covers the prepare_continue contract: missing project / never-ran /
unknown workflow refuse; a done project re-opens with the new workflow's phase
keys cleared from completed_phases and prior outputs stashed in
carryover_outputs; workflow.txt is rewritten; archived projects are skipped by
discovery; the CLI exposes the flag."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPrepareContinue(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _mkproject(self, slug, state=None, workflow_txt=None):
        app_dir = os.path.join(self.root, slug)
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        with open(os.path.join(app_dir, "initial_prompt", "initial_prompt.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("Research the market for a note-taking app.\n")
        if state is not None:
            with open(os.path.join(app_dir, "agent_state.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(state, fh)
        if workflow_txt:
            with open(os.path.join(app_dir, "workflow.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(workflow_txt + "\n")
        return app_dir

    def _read_state(self, app_dir):
        with open(os.path.join(app_dir, "agent_state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_missing_project_refuses(self):
        rc, target = orch.prepare_continue(self.root, "nope", "app_build")
        self.assertEqual(rc, 2)
        self.assertIsNone(target)

    def test_never_ran_project_refuses(self):
        self._mkproject("fresh")
        rc, target = orch.prepare_continue(self.root, "fresh", "app_build")
        self.assertEqual(rc, 2)
        self.assertIsNone(target)

    def test_unknown_workflow_refuses(self):
        self._mkproject("done-research",
                        {"done": True, "error": None, "workflow": "research",
                         "completed_phases": ["research_scope"],
                         "phase_outputs": {"research_scope": "findings"}})
        rc, target = orch.prepare_continue(self.root, "done-research",
                                           "no_such_workflow")
        self.assertEqual(rc, 2)
        self.assertIsNone(target)

    def test_done_research_continues_into_build(self):
        """The headline scenario: research-only project continued into a full
        build. Prior outputs become carryover context, done clears, and
        workflow.txt points at the new workflow."""
        app_dir = self._mkproject(
            "staged",
            {"done": True, "error": None, "workflow": "research",
             "completed_phases": ["research_scope", "research_synthesis"],
             "phase_outputs": {"research_scope": "scope notes",
                               "research_synthesis": "synthesis notes"},
             "consensus_status": {}, "vote_results": {}},
            workflow_txt="research")
        rc, target = orch.prepare_continue(self.root, "staged", "app_build")
        self.assertEqual(rc, 0)
        self.assertEqual(target, "staged")
        st = self._read_state(app_dir)
        self.assertFalse(st["done"])
        self.assertIsNone(st["error"])
        self.assertEqual(st["workflow"], "app_build")
        # Prior-workflow outputs whose keys app_build does not re-run ride
        # along as carryover context.
        wf = orch.wflib.load_workflow("app_build", HERE)
        new_keys = {p.key for p in wf.phases}
        for key, val in (("research_scope", "scope notes"),
                         ("research_synthesis", "synthesis notes")):
            if key not in new_keys:
                self.assertEqual(st["carryover_outputs"].get(key), val)
        with open(os.path.join(app_dir, "workflow.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "app_build")

    def test_shared_phase_keys_rerun(self):
        """Phase keys the new workflow shares with the old one must re-run:
        dropped from completed_phases and their records cleared."""
        wf = orch.wflib.load_workflow("app_build", HERE)
        shared = wf.phases[0].key
        app_dir = self._mkproject(
            "shared",
            {"done": True, "error": None, "workflow": "prototype",
             "completed_phases": [shared, "only_in_old"],
             "phase_outputs": {shared: "stale", "only_in_old": "kept"},
             "consensus_status": {shared: "consensus"},
             "vote_results": {shared: "yes"}})
        rc, target = orch.prepare_continue(self.root, "shared", "app_build")
        self.assertEqual(rc, 0)
        self.assertEqual(target, "shared")
        st = self._read_state(app_dir)
        self.assertNotIn(shared, st["completed_phases"])
        self.assertNotIn(shared, st["phase_outputs"])
        self.assertNotIn(shared, st["consensus_status"])
        self.assertNotIn(shared, st["vote_results"])
        # The old-only phase stays completed and its output carries over.
        self.assertIn("only_in_old", st["completed_phases"])
        self.assertEqual(st["carryover_outputs"]["only_in_old"], "kept")

    def test_aborted_project_can_continue(self):
        app_dir = self._mkproject(
            "aborted",
            {"done": False, "error": "agent exploded", "workflow": "research",
             "completed_phases": [], "phase_outputs": {}})
        rc, target = orch.prepare_continue(self.root, "aborted", "app_spec")
        self.assertEqual(rc, 0)
        self.assertEqual(target, "aborted")
        st = self._read_state(app_dir)
        self.assertIsNone(st["error"])
        self.assertFalse(st["done"])
        self.assertEqual(st["workflow"], "app_spec")


class TestArchivedDiscovery(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _mkproject(self, slug, archived=False):
        app_dir = os.path.join(self.root, slug)
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        with open(os.path.join(app_dir, "initial_prompt", "initial_prompt.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("prompt\n")
        if archived:
            open(os.path.join(app_dir, ".orch_archived"), "w").close()
        return app_dir

    def test_archived_projects_hidden_from_discovery(self):
        self._mkproject("active")
        self._mkproject("hidden", archived=True)
        self.assertEqual(orch.find_apps(self.root), ["active"])

    def test_unarchive_restores_discovery(self):
        app_dir = self._mkproject("was-hidden", archived=True)
        self.assertEqual(orch.find_apps(self.root), [])
        os.remove(os.path.join(app_dir, ".orch_archived"))
        self.assertEqual(orch.find_apps(self.root), ["was-hidden"])


class TestContinueWithCLI(unittest.TestCase):
    def test_flag_in_help(self):
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"), "--help"],
            capture_output=True, text=True, timeout=60)
        self.assertIn("--continue-with", out.stdout)

    def test_requires_app_slug(self):
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"),
             "--continue-with", "app_build"],
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--continue-with requires --app/--project", out.stderr)

    def test_conflicts_with_resume(self):
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"),
             "--resume", "x", "--continue-with", "app_build"],
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--continue-with conflicts with --resume", out.stderr)


if __name__ == "__main__":
    unittest.main()
