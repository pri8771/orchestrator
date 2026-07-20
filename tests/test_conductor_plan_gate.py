"""V3 7.12: plan gate, edited execution, activity, and provenance proofs."""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import artifacts as artlib
import conductor as cond
import conductor_permissions as cplib
import conductor_plan as cplan
import conductor_routing as crlib
import sections as seclib


def _quiet(*_args):
    pass


class PlanGateBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sid = "proj/ideas/chat-1"
        self.session_dir = os.path.join(self.root, self.sid)
        self.project_dir = os.path.join(self.root, "proj")
        os.makedirs(self.session_dir)
        self.meta = {"id": "idea-1", "artifact_type": "idea",
                     "content_hash": "content-v1", "lineage": [],
                     "hop_count": 0, "status": "final",
                     "source": {"section": "ideas", "session": "chat-1"}}
        raw = {"artifact_routes": {}, "rules": [{
            "match": {"artifact_type": "idea", "source_section": "ideas"},
            "strategy": "every", "targets": ["research", "planning"],
            "rule_id": "fanout"}]}
        rule, error = crlib.validate_rule(raw["rules"][0])
        self.assertIsNone(error)
        self.config = crlib.RouteConfig(rules=[rule])
        self.minted = []
        self.effected = set()
        self._real_list = artlib.list_artifacts
        self._real_lineage = artlib.lineage_index

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _section(self, name, escalated=False):
        return seclib.Section(
            id=name, title=name.title(), workflow=None,
            workflow_name="research", default_mode="chat",
            artifact_types_emitted=[], artifact_types_accepted=[],
            dod_tier="standard",
            capabilities={"writes": "workspace", "exec": False,
                          "external": bool(escalated)})

    def _list(self, path, type=None, **kwargs):
        if os.path.realpath(path) == os.path.realpath(self.project_dir):
            return self._real_list(path, type=type, **kwargs)
        return [self.meta] if type in (None, "idea") else []

    def _mint(self, root, project, section, request, **_kwargs):
        self.minted.append((section, dict(request)))
        self.effected.add(request["route_id"])
        return os.path.join(root, project, section,
                            "routed-%s" % request["route_id"])

    def _run(self, state, dial="full_auto", *, feedback=False,
             capability=False, eval_crash=None):
        state["oversight"] = {"dial": dial}
        targets = ["research", "ideas"] if feedback \
            else ["research", "planning"]
        raw = {"artifact_routes": {}, "rules": [{
            "match": {"artifact_type": "idea", "source_section": "ideas"},
            "strategy": "every", "targets": targets, "rule_id": "fanout"}]}
        rule, error = crlib.validate_rule(raw["rules"][0])
        self.assertIsNone(error)
        config = crlib.RouteConfig(rules=[rule])

        def load_section(name, *_args, **_kwargs):
            return self._section(name, escalated=(capability and name == targets[-1]))

        patches = [
            unittest.mock.patch.object(crlib, "load_route_config",
                                       lambda *_a, **_k: config),
            unittest.mock.patch.object(artlib, "list_artifacts", self._list),
            unittest.mock.patch.object(artlib, "is_admissible",
                                       lambda *_a, **_k: True),
            unittest.mock.patch.object(
                artlib, "lineage_index",
                lambda path, **_k: ({"by_id": {"idea-1": self.meta}}
                                    if os.path.realpath(path) ==
                                    os.path.realpath(self.session_dir)
                                    else self._real_lineage(path, **_k))),
            unittest.mock.patch.object(seclib, "load_section", load_section),
            unittest.mock.patch("sessions.mint_delegation_session", self._mint),
            unittest.mock.patch("sessions.scan_effected_routes",
                                lambda *_a: set(self.effected)),
            unittest.mock.patch.object(cond, "snapshot", lambda *_a, **_k: None),
        ]
        if eval_crash is not None:
            patches.append(unittest.mock.patch.object(cond, "_eval_crash",
                                                       eval_crash))
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7]:
            if eval_crash is None:
                return cond.route_engine(self.root, state, [self.sid], emit=_quiet)
            with patches[8]:
                return cond.route_engine(self.root, state, [self.sid], emit=_quiet)


class TestPlanGateMatrix(PlanGateBase):
    def test_dial_matrix_executes_or_pends_all_twelve_shapes(self):
        expectations = {
            "full_auto": (True, True, False),
            "suggest_only": (False, False, False),
            "gated": (False, False, False),
            "loops_gated": (True, False, False),
        }
        for dial, expected in expectations.items():
            for (label, feedback, capability), should_execute in zip(
                    (("forward", False, False),
                     ("feedback", True, False),
                     ("capability", False, True)), expected):
                with self.subTest(dial=dial, shape=label):
                    # Each matrix cell gets a fresh durable workspace.
                    self.tearDown()
                    self.setUp()
                    state = cond.default_state()
                    self._run(state, dial, feedback=feedback,
                              capability=capability)
                    plans = [a for a in cplib.read_pending(self.root)
                             if a.get("kind") == "plan"]
                    self.assertEqual(len(self.minted), 2 if should_execute else 0)
                    self.assertEqual(len(plans), 0 if should_execute else 1)
                    records = [r for r in cond.read_ledger(self.root) if r]
                    self.assertTrue(any(r.get("decision") == "plan_published"
                                        for r in records))
                    if capability:
                        self.assertFalse(should_execute,
                                         "capability floor beats every dial")

    def test_full_auto_publishes_and_ledgers_before_first_effect(self):
        order = []

        def mint(root, project, section, request, **kwargs):
            records = [r for r in cond.read_ledger(self.root) if r]
            self.assertTrue(any(r.get("decision") == "plan_published"
                                for r in records))
            metas = self._real_list(self.project_dir, type="plan")
            self.assertEqual(len(metas), 1)
            order.append(section)
            return PlanGateBase._mint(
                self, root, project, section, request, **kwargs)

        with unittest.mock.patch.object(self, "_mint", side_effect=mint):
            self._run(cond.default_state(), "full_auto")
        self.assertEqual(order, ["research", "planning"])


class TestEditedPlanExecution(PlanGateBase):
    def test_edited_plan_is_exactly_what_executes_and_double_approve_is_once(self):
        state = cond.default_state()
        self._run(state, "gated")
        action = next(a for a in cplib.read_pending(self.root)
                      if a.get("kind") == "plan")
        plan_id = action["payload"]["plan_id"]
        edited_steps = [
            {"id": "human-1", "title": "Check sources",
             "target_section": "documentation",
             "expected_artifact_type": "research_brief"},
            {"id": "human-2", "title": "Prepare release",
             "target_section": "qa",
             "expected_artifact_type": "release_checklist"},
        ]
        edited_body = cplan.render_plan_body(edited_steps, "Human plan")
        os.makedirs(cplib.approvals_dir(self.root), exist_ok=True)
        with open(os.path.join(cplib.approvals_dir(self.root),
                               "%s.edit" % plan_id), "w",
                  encoding="utf-8") as fh:
            fh.write(edited_body)
        state = cond.load_conductor_state(self.root)
        self._run(state, "gated")
        self.assertEqual([target for target, _request in self.minted],
                         ["documentation", "qa"])
        refs = [request.get("plan_ref") for _target, request in self.minted]
        self.assertEqual([ref["step_id"] for ref in refs],
                         ["human-1", "human-2"])
        self.assertTrue(all(ref["plan_version"] == 2 for ref in refs))
        self.assertTrue(all(ref["plan_id"] != plan_id for ref in refs))
        edited_meta = artlib.load_meta(self.project_dir, refs[0]["plan_id"])
        self.assertEqual(edited_meta["supersedes"], plan_id)
        self.assertEqual(edited_meta["source"]["session"], "human")

        records = [r for r in cond.read_ledger(self.root) if r]
        executed = [r for r in records if r.get("decision") == "plan_executed"]
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0]["detail"]["plan_version"], 2)
        route_refs = [r["detail"].get("plan_ref") for r in records
                      if r.get("decision") == "route_approved"
                      and r.get("detail", {}).get("plan_ref")]
        self.assertEqual({ref["plan_version"] for ref in route_refs}, {2})
        activity = cplan.read_activity(cond.conductor_dir(self.root))
        self.assertEqual(len(activity), 4)
        self.assertEqual({r["plan_version"] for r in activity}, {2})
        self.assertEqual({r["step_id"] for r in activity},
                         {"human-1", "human-2"})
        def keys(records):
            return {(r["plan_id"], r["plan_version"], r["step_id"],
                     r["status"]) for r in records}
        self.assertEqual(keys(cplan.read_activity(self.session_dir)),
                         keys(activity))

        # A late duplicate approval cannot resurrect a removed pending action.
        with open(os.path.join(cplib.approvals_dir(self.root),
                               "%s.ok" % plan_id), "w"):
            pass
        self._run(state, "gated")
        self.assertEqual(len(self.minted), 2)
        self.assertEqual(len([r for r in cond.read_ledger(self.root) if r
                              and r.get("decision") == "plan_executed"]), 1)

    def test_reject_is_reasoned_terminal_and_never_repends(self):
        state = cond.default_state()
        self._run(state, "gated")
        action = next(a for a in cplib.read_pending(self.root)
                      if a.get("kind") == "plan")
        path = os.path.join(cplib.approvals_dir(self.root),
                            "%s.changes" % action["action_id"])
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Wrong order")
        self._run(state, "gated")
        self._run(state, "gated")
        self.assertEqual(self.minted, [])
        self.assertFalse(any(a.get("kind") == "plan"
                             for a in cplib.read_pending(self.root)))
        rejected = [r for r in cond.read_ledger(self.root) if r
                    and r.get("decision") == "plan_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["detail"]["reason"], "Wrong order")


class TestPlanCrashResume(PlanGateBase):
    def test_crash_after_first_effect_resumes_without_duplicate_activity(self):
        state = cond.default_state()
        crashed = {"done": False}

        def crash(stage):
            if stage == "post_act_pre_record" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("simulated SIGKILL boundary")

        with self.assertRaisesRegex(RuntimeError, "SIGKILL"):
            self._run(state, "full_auto", eval_crash=crash)
        self.assertEqual(len(self.minted), 1)
        self._run(state, "full_auto")
        # First route is recovered by route-id probe; only the second mints.
        self.assertEqual(len(self.minted), 2)
        activity = cplan.read_activity(cond.conductor_dir(self.root))
        keys = [(r["step_id"], r["status"]) for r in activity]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {
            ("step-1", "started"), ("step-1", "completed"),
            ("step-2", "started"), ("step-2", "completed")})


class TestPlanArtifactContract(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.registry = artlib.load_registry(os.path.dirname(artlib.__file__))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_unparseable_plan_is_refused_without_a_partial_artifact(self):
        errors = []
        aid = artlib.publish(
            self.root, "# no machine-readable steps",
            {"type": "plan", "title": "Opaque",
             "source": {"section": "ideas", "session": "conductor"}},
            self.registry, on_error=errors.append, consensus=True)
        self.assertIsNone(aid)
        self.assertTrue(any("exactly one" in error for error in errors))
        self.assertEqual(artlib.list_artifacts(self.root), [])

    def test_plan_ref_round_trip_and_intent_stale_after_human_edit(self):
        steps = [{"id": "s1", "title": "Research",
                  "target_section": "research",
                  "expected_artifact_type": "research_brief"}]
        body = cplan.render_plan_body(steps)
        plan = artlib.publish(
            self.root, body,
            {"type": "plan", "title": "Plan",
             "source": {"section": "ideas", "session": "conductor"}},
            self.registry, consensus=True)
        output = artlib.publish(
            self.root, "Evidence",
            {"type": "idea", "title": "Output",
             "source": {"section": "research", "session": "worker",
                        "plan_ref": {"plan_id": plan, "plan_version": 1,
                                     "step_id": "s1"}}},
            self.registry, consensus=True)
        output_meta = artlib.load_meta(self.root, output)
        self.assertEqual(output_meta["plan_ref"], {
            "plan_id": plan, "plan_version": 1, "step_id": "s1"})
        self.assertFalse(artlib.is_intent_stale(self.root, output_meta))
        edited = artlib.publish(
            self.root, cplan.render_plan_body(steps, "Edited"),
            {"type": "plan", "title": "Plan",
             "source": {"section": "ideas", "session": "human"}},
            self.registry, supersedes=plan, consensus=True)
        self.assertIsNotNone(edited)
        self.assertTrue(artlib.is_intent_stale(self.root, output_meta))

    def test_activity_is_capped_redacted_deduped_and_never_raises(self):
        record = {"plan_id": "p", "plan_version": 1, "step_id": "s",
                  "actor": "token=secret-value " + ("x" * 8000),
                  "action": "route", "status": "started",
                  "artifact_ids": ["a"]}
        self.assertTrue(cplan.append_activity(self.root, record))
        self.assertTrue(cplan.append_activity(self.root, record))
        path = os.path.join(self.root, cplan.ACTIVITY_FILENAME)
        with open(path, "rb") as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 1)
        self.assertLess(len(lines[0]), 3500)
        self.assertNotIn(b"secret-value", lines[0])
        self.assertFalse(cplan.append_activity("/dev/null/nope", record))
