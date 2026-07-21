"""V3 7.6: oversight dial matrix, durable undo, and guarded session kill."""

import json
import os
import shutil
import signal
import tempfile
import unittest
import unittest.mock

import artifacts
import conductor as cond
import conductor_permissions as cp
import conductor_routing as cr
import events
import sections
import sessions


class RouteHarness:
    def make_root(self):
        root = tempfile.mkdtemp()
        sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(root, sid))
        return root, sid

    def run_route(self, root, sid, state, meta, targets, capabilities=None,
                  mint=None):
        rules = [{"artifact_type": "idea", "source_section": None,
                  "strategy": "every" if len(targets) > 1 else "one",
                  "targets": list(targets), "hop_budget": 9,
                  "rule_id": "rule-main"}]
        cfg = cr.RouteConfig(rules=rules)
        by_id = {meta["id"]: meta}
        for aid, ancestor in (meta.get("ancestor_metas") or {}).items():
            by_id[aid] = ancestor

        def list_artifacts(app_dir, **kwargs):
            if app_dir == os.path.join(root, "proj"):
                assert kwargs.get("type") == "plan" \
                    or kwargs.get("status") == "final"
                return []
            assert app_dir == os.path.join(root, sid)
            assert kwargs.get("status") == "final"
            return [meta]

        def lineage_index(app_dir, **_kwargs):
            if app_dir == os.path.join(root, "proj"):
                return {"by_id": {}}
            assert app_dir == os.path.join(root, sid)
            return {"by_id": by_id}

        def load_config(sections_dir, section, project_dir, **_kwargs):
            assert section == "ideas"
            assert project_dir == os.path.join(root, "proj")
            return cfg

        def load_section(name, *_args, **_kwargs):
            caps = (capabilities or {}).get(name, {
                "writes": "workspace", "exec": False, "external": False})
            return sections.Section(
                id=name, title=name, workflow=None, workflow_name="inline",
                default_mode="chat", artifact_types_emitted=[],
                artifact_types_accepted=[], dod_tier="standard",
                capabilities=caps)

        minted = [] if mint is None else None

        def default_mint(_root, _project, target, request, **_kwargs):
            minted.append((target, request["route_id"]))
            return os.path.join(root, "proj", target, request["route_id"])

        with unittest.mock.patch.object(cr, "load_route_config", load_config), \
                unittest.mock.patch.object(artifacts, "list_artifacts",
                                           list_artifacts), \
                unittest.mock.patch.object(artifacts, "is_admissible",
                                           lambda app_dir, item, **kwargs:
                                           app_dir == os.path.join(root, sid)
                                           and item is meta
                                           and "index" in kwargs), \
                unittest.mock.patch.object(artifacts, "lineage_index",
                                           lineage_index), \
                unittest.mock.patch.object(sections, "load_section",
                                           load_section), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    mint or default_mint), \
                unittest.mock.patch("sessions.scan_effected_routes",
                                    lambda r, p, s: set()):
            cond.route_engine(root, state, [sid], emit=lambda *_args: None)
        return minted


class TestDialMatrix(unittest.TestCase, RouteHarness):
    def test_dial_matrix(self):
        expected = {
            "full_auto": {"forward": "auto", "feedback": "auto",
                          "capability": "pending"},
            "suggest_only": {"forward": "pending", "feedback": "pending",
                             "capability": "pending"},
            "gated": {"forward": "pending", "feedback": "pending",
                      "capability": "pending"},
            "loops_gated": {"forward": "auto", "feedback": "pending",
                            "capability": "pending"},
        }
        for dial, cases in expected.items():
            for case, outcome in cases.items():
                with self.subTest(dial=dial, case=case):
                    root, sid = self.make_root()
                    try:
                        state = cond.default_state()
                        state["oversight"] = {"dial": dial}
                        meta = {"id": "a1", "artifact_type": "idea",
                                "content_hash": "h1", "lineage": [],
                                "hop_count": 0, "status": "final"}
                        caps = None
                        if case == "feedback":
                            meta["lineage"] = ["old"]
                            meta["ancestor_metas"] = {
                                "old": {"source": {"section": "research"},
                                        "content_hash": "old-h"}}
                        if case == "capability":
                            caps = {"research": {
                                "writes": "workspace", "exec": False,
                                "external": True}}
                        minted = self.run_route(
                            root, sid, state, meta, ["research"], caps)
                        pending = cp.read_pending(root)
                        self.assertEqual(bool(minted), outcome == "auto")
                        self.assertEqual(bool(pending), outcome == "pending")
                        decisions = [r["decision"] for r in cond.read_ledger(root)
                                     if r]
                        self.assertIn("route_approved" if outcome == "auto"
                                      else "approval_requested", decisions)
                        proposed = events.read_events(
                            os.path.join(root, sid), kinds=["route_proposed"])
                        if outcome == "pending":
                            self.assertEqual(len(proposed), 1)
                            self.assertEqual(proposed[0]["route_id"],
                                             pending[0]["route_id"])
                            self.assertEqual(proposed[0]["status"],
                                             "needs_approval")
                        else:
                            self.assertEqual(proposed, [])
                    finally:
                        shutil.rmtree(root, ignore_errors=True)

    def test_feedback_step_pends_the_whole_multi_step_plan(self):
        root, sid = self.make_root()
        try:
            state = cond.default_state()
            meta = {"id": "a1", "artifact_type": "idea",
                    "content_hash": "h1", "lineage": ["old"],
                    "ancestor_metas": {
                        "old": {"source": {"section": "research"},
                                "content_hash": "old-h"}},
                    "hop_count": 1, "status": "final"}
            minted = self.run_route(
                root, sid, state, meta, ["research", "planning"])
            self.assertEqual(minted, [])
            pending = cp.read_pending(root)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["kind"], "plan")
            self.assertEqual(
                [step["target_section"] for step in
                 pending[0]["payload"]["step_summary"]],
                ["research", "planning"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestOversightPersistence(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_dial_falls_back_loudly_once(self):
        legacy = cond.default_state()
        legacy.pop("oversight")
        cond.save_conductor_state(self.root, legacy)
        state = cond.load_conductor_state(self.root)
        cond.sync_oversight_from_disk(self.root, state, emit=lambda *_a: None)
        self.assertEqual(cond.oversight_dial(state), "loops_gated")
        first = [r for r in cond.read_ledger(self.root) if r]
        self.assertEqual([r["decision"] for r in first],
                         ["oversight_fallback"])
        reloaded = cond.load_conductor_state(self.root)
        cond.sync_oversight_from_disk(
            self.root, reloaded, emit=lambda *_a: None)
        self.assertEqual(len([r for r in cond.read_ledger(self.root) if r]), 1)

    def test_live_dial_change_is_read_back_and_ledgered(self):
        state = cond.default_state()
        cond.save_conductor_state(self.root, state)
        with open(cond.state_path(self.root), encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["oversight"] = {"dial": "gated"}
        with open(cond.state_path(self.root), "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        cond.sync_oversight_from_disk(self.root, state, emit=lambda *_a: None)
        self.assertEqual(cond.oversight_dial(state), "gated")
        self.assertEqual(cond.read_ledger(self.root)[-1]["decision"],
                         "oversight_changed")

    def test_gui_request_changes_dial_without_rewriting_state_cache(self):
        state = cond.default_state()
        state["sessions"] = {"demo/ideas/chat-1": "digest"}
        cond.save_conductor_state(self.root, state)
        request = os.path.join(cond.conductor_dir(self.root),
                               cond.OVERSIGHT_REQUEST_FILENAME)
        with open(request, "w", encoding="utf-8") as fh:
            json.dump({"dial": "suggest_only"}, fh)
        cond.sync_oversight_from_disk(self.root, state, emit=lambda *_a: None)
        self.assertEqual(cond.oversight_dial(state), "suggest_only")
        self.assertFalse(os.path.exists(request))
        self.assertEqual(state["ledger_cursor"], 1)
        self.assertEqual(state["sessions"], {"demo/ideas/chat-1": "digest"})


class TestUndoLifecycle(unittest.TestCase, RouteHarness):
    def test_reject_reason_and_duplicate_approval_execute_once(self):
        root, sid = self.make_root()
        try:
            state = cond.default_state()
            state["oversight"] = {"dial": "gated"}
            meta = {"id": "a1", "artifact_type": "idea",
                    "content_hash": "h1", "lineage": [], "hop_count": 0,
                    "status": "final"}
            self.run_route(root, sid, state, meta, ["research"])
            rid = cp.read_pending(root)[0]["route_id"]
            with open(os.path.join(cp.approvals_dir(root), rid + ".changes"),
                      "w", encoding="utf-8") as fh:
                fh.write("Needs legal review")
            self.run_route(root, sid, state, meta, ["research"])
            denied = [r for r in cond.read_ledger(root) if r and
                      r.get("decision") == "route_denied"]
            self.assertEqual(len(denied), 1)
            self.assertEqual(denied[0]["detail"]["reason"],
                             "Needs legal review")

            # A distinct route proves double approval separately.
            meta2 = dict(meta, id="a2", content_hash="h2")
            self.run_route(root, sid, state, meta2, ["research"])
            rid2 = cp.read_pending(root)[0]["route_id"]
            open(os.path.join(cp.approvals_dir(root), rid2 + ".ok"), "w").close()
            minted = []

            def mint(*_args, **_kwargs):
                minted.append(1)
                return "/session"

            self.run_route(root, sid, state, meta2, ["research"], mint=mint)
            open(os.path.join(cp.approvals_dir(root), rid2 + ".ok"), "w").close()
            self.run_route(root, sid, state, meta2, ["research"], mint=mint)
            self.assertEqual(minted, [1])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_do_not_route_survives_restart_and_suppresses_new_version(self):
        root, sid = self.make_root()
        try:
            state = cond.default_state()
            state["oversight"] = {"dial": "gated"}
            meta = {"id": "a1", "artifact_type": "idea",
                    "content_hash": "h1", "lineage": [], "hop_count": 0,
                    "status": "final"}
            self.run_route(root, sid, state, meta, ["research"])
            rid = cp.read_pending(root)[0]["route_id"]
            open(os.path.join(cp.approvals_dir(root),
                              rid + ".do_not_route"), "w").close()
            self.run_route(root, sid, state, meta, ["research"])
            reloaded = cond.load_conductor_state(root)
            newer = dict(meta, content_hash="h2")
            minted = self.run_route(root, sid, reloaded, newer, ["research"])
            self.assertEqual(minted, [])
            self.assertEqual(cp.read_pending(root), [])
            self.assertIn("route_suppressed", [
                r["decision"] for r in cond.read_ledger(root) if r])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestKillSession(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sid = "proj/research/chat-routed"
        self.session_dir = os.path.join(self.root, self.sid)
        os.makedirs(self.session_dir)
        with open(os.path.join(self.session_dir, "delegation.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"request": {"route_id": "rid"}}, fh)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dead_pid_is_cleared_without_a_signal(self):
        sessions.write_pidfile(self.session_dir, 424242)
        from orchestrator import encode_lock_name
        lock_dir = os.path.join(self.root, ".orch-locks")
        os.makedirs(lock_dir)
        lock_path = os.path.join(
            lock_dir, encode_lock_name(self.sid) + ".lock")
        with open(lock_path, "w", encoding="utf-8") as fh:
            fh.write("pid=424242 host=test\n")
        calls = []

        def dead(pid, sig):
            calls.append((pid, sig))
            raise ProcessLookupError()

        result = cond.kill_spawned_session(
            self.root, self.sid, kill_fn=dead,
            command_fn=lambda _pid: "")
        self.assertEqual(result["status"], "not_running")
        self.assertEqual(calls, [(424242, 0)])
        self.assertFalse(os.path.exists(sessions.run_pid_path(self.session_dir)))
        self.assertFalse(os.path.exists(lock_path))

    def test_recycled_foreign_pid_is_never_signaled(self):
        sessions.write_pidfile(self.session_dir, 777)
        calls = []

        def alive(pid, sig):
            calls.append((pid, sig))

        result = cond.kill_spawned_session(
            self.root, self.sid, grace_seconds=0, kill_fn=alive,
            command_fn=lambda _pid: "/bin/sleep 999")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(calls, [(777, 0)])
        self.assertNotIn(signal.SIGTERM, [sig for _pid, sig in calls])
        self.assertTrue(os.path.exists(sessions.run_pid_path(self.session_dir)))

    def test_matching_live_session_gets_liveness_probe_before_term(self):
        sessions.write_pidfile(self.session_dir, 888)
        calls = []
        alive = [True]

        def kill(pid, sig):
            calls.append((pid, sig))
            if sig == 0 and not alive[0]:
                raise ProcessLookupError()
            if sig == signal.SIGTERM:
                alive[0] = False

        command = ("/usr/bin/python3 /repo/orchestrator.py --root %s --app %s"
                   % (self.root, self.sid))
        result = cond.kill_spawned_session(
            self.root, self.sid, kill_fn=kill,
            command_fn=lambda _pid: command, sleep_fn=lambda _n: None)
        self.assertEqual(result["status"], "killed")
        self.assertEqual(calls[0], (888, 0))
        self.assertIn((888, signal.SIGTERM), calls)
        self.assertFalse(os.path.exists(sessions.run_pid_path(self.session_dir)))


if __name__ == "__main__":
    unittest.main()
