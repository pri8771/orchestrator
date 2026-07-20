import json
import os
import tempfile
import unittest
from unittest import mock

import conductor


class ConductorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, relative, body):
        path = os.path.join(self.root, relative)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def read(self, relative):
        with open(os.path.join(self.root, relative), encoding="utf-8") as fh:
            return fh.read()

    def route(self, route_id):
        cursor = conductor.ledger_append(self.root, {
            "v": 1, "ts": 1, "stage": "acting", "session": "p/ideas/c",
            "route_id": route_id, "decision": "route_approved"})
        state = conductor.default_state()
        state["routed"][route_id] = True
        state["ledger_cursor"] = cursor
        conductor.save_conductor_state(self.root, state)
        return cursor

    def test_existing_repo_preserved_and_extra_ignore_rule_kept(self):
        self.assertTrue(conductor.ensure_workspace_repo(self.root)[0])
        self.write("kept.txt", "one")
        conductor._git(self.root, "add", "-A")
        conductor._git(self.root, "-c", "user.name=T", "-c",
                       "user.email=t@example.test", "commit", "-qm", "user")
        before = conductor._git(self.root, "rev-list", "--count", "HEAD")[1]
        self.write(".gitignore", "user-only/\n")
        self.assertTrue(conductor.ensure_workspace_repo(self.root)[0])
        after = conductor._git(self.root, "rev-list", "--count", "HEAD")[1]
        self.assertEqual(before, after)
        rules = self.read(".gitignore")
        self.assertIn("user-only/", rules)
        self.assertIn(".conductor/approvals/", rules)
        self.assertIn("*_api_key", rules)

    def test_ignored_paths_never_committed(self):
        self.assertTrue(conductor.ensure_workspace_repo(self.root)[0])
        paths = [".stream/t.ndjson", ".orch-locks/a.lock",
                 ".conductor/approvals/a.json",
                 ".orchestrator_runtime/docs/worktree/file",
                 "provider_api_key", ".env"]
        for path in paths:
            self.write(path, "secret-or-scratch")
        self.write("tracked.txt", "yes")
        tag = conductor.snapshot(self.root, "test", 0)
        self.assertIsNotNone(tag)
        tracked = conductor._git(self.root, "ls-tree", "-r", "--name-only",
                                 tag)[1].splitlines()
        self.assertIn("tracked.txt", tracked)
        for path in paths:
            self.assertNotIn(path, tracked)

    def test_clean_tree_no_empty_commit_or_duplicate_tag(self):
        self.write("tracked.txt", "yes")
        first = conductor.snapshot(self.root, "first", 0)
        commits = conductor._git(self.root, "rev-list", "--count", "HEAD")[1]
        second = conductor.snapshot(self.root, "second", 0)
        self.assertEqual(first, second)
        self.assertEqual(commits, conductor._git(
            self.root, "rev-list", "--count", "HEAD")[1])
        self.assertEqual(1, len(conductor._git(
            self.root, "tag", "--list", "conductor/*")[1].splitlines()))

    def test_snapshot_failure_is_nonfatal_logged_and_visible(self):
        errors = []
        with mock.patch.object(conductor, "ensure_workspace_repo",
                               return_value=(False, "index.lock held")):
            self.assertIsNone(conductor.snapshot(
                self.root, "routing-wave", 0, errors.append))
        records = conductor.read_ledger(self.root)
        self.assertEqual("snapshot_failed", records[-1]["decision"])
        self.assertIn("index.lock held", records[-1]["detail"]["error"])
        event_path = os.path.join(self.root, ".conductor", "events.jsonl")
        self.assertTrue(os.path.exists(event_path))
        self.assertTrue(errors)

    def test_rollback_integration_replays_routed_ids_and_preserves_later_tag(self):
        self.write("artifact.txt", "v1")
        c1 = self.route("route-one")
        middle = conductor.snapshot(self.root, "routing-wave", c1)
        self.write("artifact.txt", "v2")
        self.write("later.txt", "later")
        c2 = self.route("route-two")
        later = conductor.snapshot(self.root, "quiescence", c2)

        result = conductor.rollback(self.root, middle, dry_run=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual("v1", self.read("artifact.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "later.txt")))
        self.assertEqual(c1, conductor.ledger_length(self.root))
        state = conductor.load_conductor_state(self.root)
        self.assertEqual({"route-one": True}, state["routed"])
        self.assertEqual(0, conductor._git(
            self.root, "diff", "--quiet", middle, "--")[0],
            "tracked workspace content must equal the selected checkpoint")
        self.assertIn(later, conductor._git(
            self.root, "tag", "--list", "conductor/*")[1].splitlines())

    def test_dry_run_mutates_nothing_and_reports_diff(self):
        self.write("artifact.txt", "v1")
        tag = conductor.snapshot(self.root, "routing-wave", 0)
        self.write("artifact.txt", "v2")
        before = self.read("artifact.txt")
        result = conductor.rollback(self.root, tag, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertIn("artifact.txt", result["diffstat"])
        self.assertEqual(before, self.read("artifact.txt"))

    def test_rollback_refuses_live_conductor_session(self):
        self.write("artifact.txt", "v1")
        tag = conductor.snapshot(self.root, "routing-wave", 0)
        session = "p/ideas/delegated"
        self.write(session + "/delegation.json", json.dumps({
            "request": {"route_id": "abc"}, "status": "running"}))
        self.write(session + "/run.pid", "%d\n" % os.getpid())
        result = conductor.rollback(self.root, tag, dry_run=False)
        self.assertFalse(result["ok"])
        self.assertIn(session, [item["session"]
                                for item in result["live_sessions"]])

    def test_commit_without_tag_is_repaired(self):
        self.write("artifact.txt", "v1")
        tag = conductor.snapshot(self.root, "routing-wave", 0)
        conductor._git(self.root, "tag", "-d", tag)
        repaired = conductor.snapshot(self.root, "routing-wave", 0)
        self.assertEqual(tag, repaired)

    def test_reset_without_truncate_is_repaired_from_journal(self):
        self.write("artifact.txt", "v1")
        c1 = self.route("one")
        tag = conductor.snapshot(self.root, "routing-wave", c1)
        self.route("two")
        conductor._atomic_json(conductor._rollback_journal_path(self.root),
                               {"tag": tag, "cursor": c1})
        conductor._git(self.root, "reset", "--hard", tag)
        state = conductor.repair_pending_rollback(self.root)
        self.assertEqual({"one": True}, state["routed"])
        self.assertEqual(c1, conductor.ledger_length(self.root))


if __name__ == "__main__":
    unittest.main()
