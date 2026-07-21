"""V3 7.13: terminal failures are typed, final, routable evidence."""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import artifacts
import conductor
import conductor_routing
import costs
import events
import orchestrator as orch
import sessions


CLASSES = ("agent_exhausted", "config_error",
           "release_gate_budget_exhausted", "crash",
           "budget_exhausted", "stalled")


class FailureFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sid = "demo/ideas/source"
        self.app_dir = os.path.join(self.root, self.sid)
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        open(os.path.join(self.root, "demo", ".orch-sections"), "a").close()
        with open(os.path.join(self.app_dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("test\n")
        self.state = orch.load_state(self.app_dir)
        self.state.update(current_phase="build", current_round=3,
                          completed_phases=["ideas", "plan"], done=False)
        orch.save_state(self.app_dir, self.state)
        costs.record_turn(self.app_dir, {
            "v": 1, "metered": True, "input_tokens": 2,
            "output_tokens": 3, "cost_micro_usd": 17})
        events.emit_event(self.app_dir, "run_finished", project="demo",
                          status="aborted")
        self.registry = artifacts.load_registry(orch.HERE)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def failures(self):
        return artifacts.list_artifacts(os.path.join(self.root, "demo"),
                                        type="failure")


class TestFailureContract(FailureFixture):
    def test_all_six_classes_are_final_complete_and_idempotent(self):
        for error_class in CLASSES:
            with self.subTest(error_class=error_class):
                message = "token=never-persist-this-secret" \
                    if error_class == "crash" else \
                    "Safe terminal category; inspect the session log."
                aid = orch.emit_failure_artifact(
                    self.app_dir, error_class,
                    message,
                    self.state, {"root": self.root})
                again = orch.emit_failure_artifact(
                    self.app_dir, error_class,
                    message,
                    self.state, {"root": self.root})
                self.assertEqual(aid, again)
                meta = artifacts.load_meta(os.path.join(self.root, "demo"), aid)
                self.assertEqual(meta["status"], "final")
                fields = meta["fields"]
                self.assertEqual(fields["error_class"], error_class)
                self.assertEqual(fields["last_checkpoint"], {
                    "phase": "build", "round": 3,
                    "completed_phases_count": 2})
                self.assertEqual(fields["cost_spent"]["cost_micro_usd"], 17)
                self.assertGreater(fields["events_cursor"], 0)
                self.assertEqual(fields["source_session"], self.sid)
                self.assertNotIn("never-persist-this-secret",
                                 json.dumps(meta))
        counts = {error_class: 0 for error_class in CLASSES}
        for meta in self.failures():
            counts[meta["fields"]["error_class"]] += 1
        self.assertEqual(counts, {error_class: 1 for error_class in CLASSES})

    def test_failure_schema_refuses_malformed_fields_before_writing(self):
        before = os.listdir(os.path.join(self.root, "demo", "artifacts")) \
            if os.path.isdir(os.path.join(self.root, "demo", "artifacts")) else []
        errors = []
        aid = artifacts.publish(
            os.path.join(self.root, "demo"), "body",
            {"type": "failure", "title": "bad", "error_class": "oops",
             "message": "bad", "last_checkpoint": {}, "cost_spent": {},
             "events_cursor": True, "source_session": "x"}, self.registry,
            on_error=errors.append)
        self.assertIsNone(aid)
        after = os.listdir(os.path.join(self.root, "demo", "artifacts")) \
            if os.path.isdir(os.path.join(self.root, "demo", "artifacts")) else []
        self.assertEqual(before, after)
        self.assertTrue(any("error_class" in error for error in errors))

    def test_broken_store_never_masks_terminal_handling_and_warns(self):
        messages = []
        with unittest.mock.patch.object(
                artifacts, "publish", side_effect=OSError("disk broken")), \
                unittest.mock.patch.object(orch, "emit", messages.append):
            self.assertIsNone(orch.emit_failure_artifact(
                self.app_dir, "crash", "safe", self.state,
                {"root": self.root}))
        self.assertEqual(len(messages), 1)
        self.assertIn("WARN terminal failure", messages[0])

    def test_crash_status_artifact_and_state_land_then_exception_propagates(self):
        with unittest.mock.patch.object(
                orch, "_run_app_pipeline",
                side_effect=RuntimeError("secret=should-not-leak")):
            with self.assertRaises(RuntimeError):
                orch.process_app({"root": self.root, "runtime": {}},
                                 self.root, self.sid)
        state = orch.load_state(self.app_dir)
        self.assertFalse(state["done"])
        self.assertIn("RuntimeError", state["error"])
        self.assertNotIn("should-not-leak", state["error"])
        finished = events.read_events(self.app_dir, kinds=("run_finished",))
        self.assertEqual(finished[-1]["status"], "crashed")
        crashes = [m for m in self.failures()
                   if m["fields"]["error_class"] == "crash"]
        self.assertEqual(len(crashes), 1)
        body = artifacts.read_body(os.path.join(self.root, "demo"),
                                   crashes[0]["id"])
        self.assertNotIn("should-not-leak", body)
        self.assertNotIn(self.root, body)


class TestTerminalPathWiring(FailureFixture):
    def _cfg(self):
        return {"root": self.root,
                "runtime": {"fleet_ledger_enabled": False,
                            "fetch_prompt_urls": False},
                "agents": {"codex_enabled": True,
                           "claude_enabled": False,
                           "gemini_enabled": False},
                "models": {}, "ios": {}}

    def test_agent_and_config_arms_keep_existing_statuses_and_publish_once(self):
        cases = ((orch.AgentError("agents down"), "aborted",
                  "agent_exhausted"),
                 (orch.AppError("bad config"), "skipped", "config_error"))
        for index, (error, status, error_class) in enumerate(cases):
            with self.subTest(error_class=error_class):
                sid = "demo/ideas/path-%d" % index
                app_dir = os.path.join(self.root, sid)
                os.makedirs(os.path.join(app_dir, "initial_prompt"))
                with open(os.path.join(app_dir, "initial_prompt",
                                       "initial_prompt.md"), "w") as fh:
                    fh.write("test\n")
                with open(os.path.join(app_dir, "workflow.txt"), "w") as fh:
                    fh.write("answer_question\n")
                with unittest.mock.patch.object(
                        orch, "process_phase", side_effect=error):
                    orch._run_app_pipeline(self._cfg(), sid, app_dir, "test")
                terminal = events.read_events(
                    app_dir, kinds=("run_finished",))[-1]
                self.assertEqual(terminal["status"], status)
                self.assertEqual(terminal["detail"], str(error))
                state = orch.load_state(app_dir)
                self.assertEqual(state["error"], str(error))
                found = [m for m in self.failures()
                         if m["fields"]["source_session"] == sid
                         and m["fields"]["error_class"] == error_class]
                self.assertEqual(len(found), 1)

    def test_release_budget_exhaustion_path_publishes_after_terminal_event(self):
        with open(os.path.join(self.app_dir, "workflow.txt"), "w") as fh:
            fh.write("answer_question\n")

        def phase(_cfg, _app, app_dir, phasedef, _prompt, _prior, state,
                  phase_index=0):
            del phase_index
            state["current_phase"] = phasedef.key
            state["release_gate_repairs"] = 2
            state.setdefault("completed_phases", []).append(phasedef.key)
            state.setdefault("phase_outputs", {})[phasedef.key] = "ok"
            orch.save_state(app_dir, state)
            return "ok"

        with unittest.mock.patch.object(orch, "process_phase", side_effect=phase), \
                unittest.mock.patch.object(
                    orch, "_release_gate_failure", return_value="broken gate"):
            orch._run_app_pipeline(
                self._cfg(), self.sid, self.app_dir, "test")
        terminal = events.read_events(
            self.app_dir, kinds=("run_finished",))[-1]
        self.assertEqual(terminal["status"], "release_gate_repair")
        found = [m for m in self.failures()
                 if m["fields"]["error_class"] ==
                 "release_gate_budget_exhausted"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["fields"]["events_cursor"],
                         orch._events_cursor(self.app_dir))


class TestFailureRouting(FailureFixture):
    def _route(self, sid=None, state=None):
        state = state or conductor.default_state()
        state["oversight"] = {"dial": "full_auto"}
        return conductor.route_engine(
            self.root, state, [sid or self.sid], emit=lambda _m: None)

    def test_default_fleet_rule_routes_project_bus_failure_to_notification(self):
        aid = orch.emit_failure_artifact(
            self.app_dir, "agent_exhausted", "Agent ladder exhausted; inspect log.",
            self.state, {"root": self.root})
        state = self._route()
        ledger = [r for r in conductor.read_ledger(self.root) if r]
        terminal = [r for r in ledger if r.get("decision") == "route_approved"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["detail"]["artifact_id"], aid)
        self.assertEqual(terminal[0]["detail"]["target"], "notification")
        self.assertEqual(len(state["routed"]), 1)
        self.assertFalse(os.path.isdir(os.path.join(
            self.root, "demo", "notification")),
            "notification is a real event sink, never a fake section")

    def test_fixer_route_mints_with_reply_to_and_delivers_reply(self):
        project_dir = os.path.join(self.root, "demo")
        with open(os.path.join(project_dir, "routing.json"), "w") as fh:
            json.dump({"rules": [{
                "rule_id": "failure-to-fixer",
                "match": {"artifact_type": "failure"}, "strategy": "one",
                "targets": ["execution"], "hop_budget": 2}]}, fh)
        aid = orch.emit_failure_artifact(
            self.app_dir, "config_error", "Configuration failed; inspect log.",
            self.state, {"root": self.root})
        self._route()
        execution = os.path.join(project_dir, "execution")
        children = [os.path.join(execution, name)
                    for name in os.listdir(execution)]
        self.assertEqual(len(children), 1)
        child = children[0]
        delegation = sessions.read_delegation(child)
        self.assertEqual(delegation["reply_to"], self.app_dir)
        self.assertEqual(delegation["request"]["artifact_id"], aid)
        reply = artifacts.publish(
            child, "Repair evidence", {"type": "postmortem", "title": "Fix",
            "source": {"section": "execution", "session": "fixer",
                       "phase": "repair", "turn": ""}}, self.registry,
            consensus=True)
        reply_meta = artifacts.load_meta(child, reply)
        self.assertTrue(sessions.deliver_reply(child, reply_meta))
        with open(os.path.join(self.app_dir, "human_inbox.txt")) as fh:
            inbox = fh.read()
        self.assertIn(reply, inbox)
        self.assertIn("postmortem", inbox)

    def test_fixer_of_fixer_consumes_hops_and_is_refused_visibly(self):
        project_dir = os.path.join(self.root, "demo")
        with open(os.path.join(project_dir, "routing.json"), "w") as fh:
            json.dump({"rules": [{
                "rule_id": "failure-to-fixer",
                "match": {"artifact_type": "failure"}, "strategy": "one",
                "targets": ["execution"], "hop_budget": 2}]}, fh)
        parent = orch.emit_failure_artifact(
            self.app_dir, "crash", "First failure", self.state,
            {"root": self.root})

        def failed_fixer(name, parent_id):
            sid = "demo/execution/%s" % name
            app_dir = os.path.join(self.root, sid)
            os.makedirs(os.path.join(app_dir, "initial_prompt"))
            with open(os.path.join(app_dir, "initial_prompt",
                                   "initial_prompt.md"), "w") as fh:
                fh.write("fix\n")
            sessions._write_delegation(app_dir, sessions._new_record(
                name, {"artifact_id": parent_id}, self.app_dir, None))
            state = orch.load_state(app_dir)
            events.emit_event(app_dir, "run_finished", project="demo",
                              status="aborted")
            return sid, orch.emit_failure_artifact(
                app_dir, "crash", "Fixer %s failed" % name, state,
                {"root": self.root})

        _sid1, child = failed_fixer("fixer-1", parent)
        sid2, grandchild = failed_fixer("fixer-2", child)
        meta = artifacts.load_meta(project_dir, grandchild)
        self.assertEqual(meta["hop_count"], 2)
        state = self._route(sid2)
        ledger = [r for r in conductor.read_ledger(self.root) if r]
        refused = [r for r in ledger
                   if r.get("decision") == conductor_routing.BUDGET_EXHAUSTED]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["detail"]["artifact_id"], grandchild)
        self.assertEqual(state["routed"].get(refused[0]["route_id"]), True)


class TestConductorFailurePaths(FailureFixture):
    def test_stall_and_workspace_budget_termination_publish_typed_artifacts(self):
        conductor_state = conductor.default_state()
        conductor._record_termination(
            self.root, conductor_state, self.sid, "stalled",
            {"stalled": True, "reason": "vote_undecided",
             "evidence": {"count": 2}}, emit=lambda _m: None)
        conductor_state["oversight"] = {"dial": "full_auto"}
        conductor.route_engine(
            self.root, conductor_state, [self.sid], emit=lambda _m: None)
        routed = [r for r in conductor.read_ledger(self.root) if r and
                  r.get("decision") == "route_approved"]
        self.assertEqual(len(routed), 1,
                         "terminal sessions still route their failure evidence")
        conductor._record_workspace_termination(
            self.root, conductor_state, "turns_exhausted",
            {"turns": {"used": 10, "cap": 10}}, emit=lambda _m: None,
            source_session=self.sid)
        failures = self.failures()
        by_class = {meta["fields"]["error_class"]: meta
                    for meta in failures}
        self.assertEqual(set(by_class), {"stalled", "budget_exhausted"})
        self.assertEqual(by_class["stalled"]["status"], "final")
        self.assertEqual(by_class["budget_exhausted"]["status"], "final")
        self.assertIn("vote undecided",
                      artifacts.read_body(
                          os.path.join(self.root, "demo"),
                          by_class["stalled"]["id"]).replace("_", " "))
        self.assertTrue(conductor_state["halted"])


if __name__ == "__main__":
    unittest.main()
