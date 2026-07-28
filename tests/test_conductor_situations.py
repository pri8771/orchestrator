"""V3 9.1b: live Situation switches are durable, scoped, and fail-open."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import artifacts
import conductor
import conductor_permissions
import conductor_termination
import docs
import workflows
from tests.test_conductor_oversight import RouteHarness


class LiveSituationSwitchTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sid = "project/ideas/session-a"
        self.app_dir = os.path.join(self.root, self.sid)
        os.makedirs(self.app_dir)
        with open(os.path.join(self.app_dir, "agent_state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"workflow": "app_build", "current_phase": "research",
                       "phase_outputs": {"research": "old evidence survives"}}, fh)
        self.state = conductor.default_state()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _resolved(ref, slots, owners):
        return {"ref": ref, "digest": ref + "-digest", "valid": True,
                "required_slots": slots, "owners": owners}

    def test_mid_run_switch_recomputes_and_cancels_stale_queue_without_kill(self):
        stale = {"action_id": "route-build", "route_id": "route-build",
                 "kind": "route", "requested_by": self.sid,
                 "target": "build", "payload": {"artifact_id": "a1",
                 "content_hash": "h", "source_section": "ideas",
                 "rule_id": "r1", "strategy": "delegate"}}
        kept = {**stale, "action_id": "route-ideas", "route_id": "route-ideas",
                "target": "ideas"}
        self.assertTrue(conductor_permissions.enqueue_pending(self.root, stale))
        self.assertTrue(conductor_permissions.enqueue_pending(self.root, kept))
        old = self._resolved("full", ["problem_statement", "implementation"],
                             ["ideas", "build"])
        new = self._resolved("brainstorm", ["problem_statement"], ["ideas"])
        scans = []

        def recompute(app_dir, project, phases, outputs, orch_dir,
                      artifact_reader, required_slots, on_warn=None,
                      human_overrides=None):
            scans.append((app_dir, list(required_slots), dict(outputs)))
            os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
            with open(os.path.join(app_dir, "docs", "GAP_REPORT.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("slots: " + ",".join(required_slots))
            return []

        with mock.patch.object(conductor, "_read_project_situation",
                               side_effect=[old, new]), \
             mock.patch.object(docs, "recompute_gap_report",
                               side_effect=recompute), \
             mock.patch("os.kill") as kill:
            conductor.sync_situations(self.root, self.state, [self.sid])
            conductor.sync_situations(self.root, self.state, [self.sid])

        kill.assert_not_called()
        self.assertEqual(scans[-1][1], ["problem_statement"])
        self.assertEqual(scans[-1][2]["research"], "old evidence survives")
        self.assertTrue(os.path.exists(os.path.join(
            self.app_dir, "agent_state.json")), "running session was preserved")
        pending_ids = {a["action_id"] for a in
                       conductor_permissions.read_pending(self.root)}
        self.assertNotIn("route-build", pending_ids)
        self.assertIn("route-ideas", pending_ids)
        cancelled = [r for r in conductor.read_ledger(self.root)
                     if r and r.get("decision") == "route_cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertIn("no longer requires", cancelled[0]["detail"]["reason"])
        self.assertEqual(cancelled[0]["detail"]["targets"], ["build"])
        changes = [r for r in conductor.read_ledger(self.root)
                   if r and r.get("decision") == "situation_changed"]
        self.assertEqual([r["detail"]["new_ref"] for r in changes],
                         ["full", "brainstorm"])

    def test_uncursored_switch_replays_as_recompute_required(self):
        conductor.ledger_append(self.root, {
            "v": 1, "ts": 1, "stage": "evaluating",
            "decision": "situation_changed",
            "detail": {"project": "project", "old_ref": "old",
                       "new_ref": "new", "digest": "new-digest"}})
        state = conductor.default_state()
        conductor.reconcile_on_start(self.root, state, emit=lambda _m: None)
        record = state["situations"]["project"]
        self.assertEqual(record["ref"], "new")
        self.assertTrue(record["needs_recompute"])
        self.assertEqual(state["ledger_cursor"], 1)

    def test_uncursored_cancellation_replay_cleans_queue_without_duplicate(self):
        action = {"action_id": "route-build", "route_id": "route-build",
                  "kind": "route", "requested_by": self.sid,
                  "target": "build", "payload": {}}
        self.assertTrue(conductor_permissions.enqueue_pending(self.root, action))
        conductor.ledger_append(self.root, {
            "v": 1, "ts": 1, "stage": "evaluating",
            "decision": "route_cancelled", "session": self.sid,
            "route_id": "route-build",
            "detail": {"project": "project", "targets": ["build"],
                       "reason": "old goal"}})
        state = conductor.default_state()
        conductor.reconcile_on_start(self.root, state, emit=lambda _m: None)
        record = self._resolved("brainstorm", ["problem_statement"], ["ideas"])
        conductor._cancel_stale_situation_routes(
            self.root, state, "project", record, lambda _m: None)
        self.assertEqual(conductor_permissions.read_pending(self.root), [])
        cancellations = [r for r in conductor.read_ledger(self.root)
                         if r and r.get("decision") == "route_cancelled"]
        self.assertEqual(len(cancellations), 1)

    def test_unknown_ref_is_visible_and_fails_open(self):
        project_dir = os.path.join(self.root, "project")
        with open(os.path.join(project_dir, "run_config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"situation": "does-not-exist"}, fh)
        shown = []
        # _read_project_situation loads from the ENGINE dir (HERE), and
        # load_situation's first act is to seed the six default situations —
        # unmocked, that materializes an untracked situations/ tree in the
        # repo checkout (a working-tree mutation CI's clean-tree guard must
        # catch). The unknown-ref path only needs the situation file to be
        # ABSENT, which seeding-as-no-op preserves.
        with mock.patch("situations.ensure_seeded",
                        lambda *a, **k: None):
            record = conductor._read_project_situation(
                self.root, "project", shown.append)
        self.assertFalse(record["valid"])
        self.assertIsNone(record["required_slots"])
        self.assertTrue(conductor.situation_allows_route(
            {"situations": {"project": record}}, "project", "build"))
        self.assertTrue(any("unknown or corrupt" in line for line in shown))
        events = os.path.join(self.root, ".conductor", "events.jsonl")
        with open(events, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline())["kind"],
                             "situation_fallback")

    def test_effect_seam_predicate_denies_removed_owner(self):
        state = {"situations": {"project": self._resolved(
            "brainstorm", ["problem_statement"], ["ideas"])}}
        self.assertTrue(conductor.situation_allows_route(
            state, "project", "ideas"))
        self.assertTrue(conductor.situation_allows_route(
            state, "project", "notification"))
        self.assertFalse(conductor.situation_allows_route(
            state, "project", "build"))

    def test_situation_with_no_known_slots_fails_open_visibly(self):
        project_dir = os.path.join(self.root, "project")
        with open(os.path.join(project_dir, "run_config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"situation": "drifted"}, fh)
        shown = []
        with mock.patch("situations.load_situation", return_value={
                "doc_slots": ["renamed-away"]}), \
             mock.patch.object(docs, "load_doc_map", return_value={
                 "slots": [{"slot_id": "problem_statement",
                            "owner_section": "ideas"}]}):
            record = conductor._read_project_situation(
                self.root, "project", shown.append)
        self.assertFalse(record["valid"])
        self.assertIsNone(record["required_slots"])
        self.assertTrue(any("no known document slots" in line for line in shown))


class ScopedGapTests(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def test_recompute_report_demands_only_new_slots(self):
        coverage = docs.recompute_gap_report(
            self.app_dir, "project",
            workflows.load_workflow("app_build").phases,
            {"research": "Evidence produced under the old Situation stays."},
            os.path.dirname(os.path.dirname(__file__)), artifacts,
            ["problem_statement"], on_warn=self.fail)
        self.assertIsNotNone(coverage)
        self.assertEqual([row["slot_id"] for row in coverage],
                         ["problem_statement"])
        with open(os.path.join(self.app_dir, "docs", "GAP_REPORT.md"),
                  encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("Problem Statement", report)
        self.assertNotIn("Target User", report)
        self.assertEqual({row["slot_id"] for row in coverage},
                         {"problem_statement"})

    def test_recompute_honors_human_overrides(self):
        # 5.5 regression: both recompute targets are in docsync's protected
        # set, and the recompute path used to bypass the override gate the
        # normal render path enforces — silently clobbering human edits.
        docs_dir = os.path.join(self.app_dir, "docs")
        os.makedirs(docs_dir)
        human = "MY hand-written gap analysis. Do not touch.\n"
        with open(os.path.join(docs_dir, "GAP_REPORT.md"), "w") as fh:
            fh.write(human)
        coverage = docs.recompute_gap_report(
            self.app_dir, "project",
            workflows.load_workflow("app_build").phases,
            {"research": "evidence"},
            os.path.dirname(os.path.dirname(__file__)), artifacts,
            ["problem_statement"], on_warn=self.fail,
            human_overrides={"docs/GAP_REPORT.md"})
        self.assertIsNotNone(coverage)
        with open(os.path.join(docs_dir, "GAP_REPORT.md")) as fh:
            self.assertEqual(fh.read(), human)   # preserved verbatim
        # The non-overridden half still renders normally.
        self.assertTrue(os.path.exists(
            os.path.join(docs_dir, "HANDOFF_BLUEPRINT.md")))

    def test_goal_ignores_open_gaps_outside_required_slots(self):
        os.makedirs(os.path.join(self.app_dir, "docs"))
        open(os.path.join(self.app_dir, "docs", "GAP_REPORT.md"), "w").close()
        gaps = [{"id": "old", "slot_id": "target_user"},
                {"id": "active", "slot_id": "problem_statement"}]
        with mock.patch.object(artifacts, "list_artifacts", return_value=gaps):
            verdict = conductor_termination.goal_predicate(
                self.app_dir, {"goal": {"doc_gap_empty": True}},
                required_slots=["problem_statement"])
        self.assertFalse(verdict["met"])
        self.assertEqual(verdict["evidence"]["doc_gap_empty"]["open_gap_ids"],
                         ["active"])
        with mock.patch.object(artifacts, "list_artifacts",
                               return_value=[gaps[0]]):
            verdict = conductor_termination.goal_predicate(
                self.app_dir, {"goal": {"doc_gap_empty": True}},
                required_slots=["problem_statement"])
        self.assertTrue(verdict["met"])

    def test_missing_gap_report_never_passes_even_when_scope_has_no_gaps(self):
        with mock.patch.object(artifacts, "list_artifacts", return_value=[]):
            verdict = conductor_termination.goal_predicate(
                self.app_dir, {"goal": {"doc_gap_empty": True}},
                required_slots=["problem_statement"])
        self.assertFalse(verdict["met"])
        self.assertEqual(verdict["evidence"]["doc_gap_empty"]["error"],
                         "no_gap_report")


class RouteEffectSeamTests(unittest.TestCase, RouteHarness):
    def test_newly_planned_stale_route_is_cancelled_before_mint(self):
        root, sid = self.make_root()
        try:
            state = conductor.default_state()
            state["oversight"] = {"dial": "full_auto"}
            state["situations"]["proj"] = {
                "ref": "brainstorm", "digest": "d", "valid": True,
                "required_slots": ["problem_statement"],
                "owners": ["ideas"], "needs_recompute": False}
            meta = {"id": "a1", "artifact_type": "idea",
                    "content_hash": "h1", "lineage": [], "hop_count": 0,
                    "status": "final"}
            minted = self.run_route(root, sid, state, meta, ["research"])
            self.assertEqual(minted, [], "removed owner reached mint effect")
            cancelled = [r for r in conductor.read_ledger(root)
                         if r and r.get("decision") == "route_cancelled"]
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(cancelled[0]["detail"]["target"], "research")
            self.assertIn("brainstorm", cancelled[0]["detail"]["reason"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
