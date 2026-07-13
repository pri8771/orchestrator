"""decisions.json structured phase handoff: parse/merge/supersede, context
injection, the worker context policy, and the _budget truncation marker."""
import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock

import orchestrator as orch
import workflows as wf


def _block(payload):
    return "```decisions-json\n%s\n```" % json.dumps(payload)


class TestParseDecisionBlocks(unittest.TestCase):
    def test_wrapper_and_single_forms(self):
        text = (_block({"decisions": [{"id": "D-1", "decision": "SwiftUI",
                                       "rationale": "native"}]})
                + "\n" + _block({"id": "D-2", "decision": "local-first"}))
        decisions, errors = orch.parse_decision_blocks(text)
        self.assertEqual(errors, [])
        self.assertEqual([d["id"] for d in decisions], ["D-1", "D-2"])
        # list fields normalized to lists even when absent
        self.assertEqual(decisions[1]["rejected_alternatives"], [])
        self.assertEqual(decisions[1]["constraints"], [])
        self.assertEqual(decisions[1]["rationale"], "")

    def test_missing_required_fields_reported_not_dropped_silently(self):
        decisions, errors = orch.parse_decision_blocks(
            _block({"decisions": [{"id": "D-1"}, {"decision": "no id"}]}))
        self.assertEqual(decisions, [])
        self.assertEqual(len(errors), 2)

    def test_last_emission_wins_per_id(self):
        text = (_block({"id": "D-1", "decision": "draft"})
                + "\n" + _block({"id": "D-1", "decision": "final"}))
        decisions, _ = orch.parse_decision_blocks(text)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "final")

    def test_scalar_list_fields_coerced(self):
        decisions, _ = orch.parse_decision_blocks(
            _block({"id": "D-1", "decision": "x",
                    "rejected_alternatives": "UIKit", "constraints": None}))
        self.assertEqual(decisions[0]["rejected_alternatives"], ["UIKit"])
        self.assertEqual(decisions[0]["constraints"], [])


class TestMergeAndSupersede(unittest.TestCase):
    def test_append_then_supersede_marks_not_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            orch.merge_decisions(d, [{"id": "D-1", "decision": "SQLite"}])
            merged = orch.merge_decisions(
                d, [{"id": "D-2", "decision": "SwiftData", "supersedes": "D-1"}])
            self.assertEqual([m["id"] for m in merged], ["D-1", "D-2"])
            old = merged[0]
            self.assertTrue(old["superseded"])
            self.assertEqual(old["superseded_by"], "D-2")
            # persisted atomically and loadable
            self.assertEqual(orch.load_decisions(d), merged)
            with open(os.path.join(d, "decisions.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("schema_version", data)

    def test_reemitted_id_replaces_but_keeps_superseded_mark(self):
        with tempfile.TemporaryDirectory() as d:
            orch.merge_decisions(d, [{"id": "D-1", "decision": "v1"},
                                     {"id": "D-2", "decision": "x", "supersedes": "D-1"}])
            merged = orch.merge_decisions(d, [{"id": "D-1", "decision": "v2 revision"}])
            d1 = next(m for m in merged if m["id"] == "D-1")
            self.assertEqual(d1["decision"], "v2 revision")
            self.assertTrue(d1["superseded"])   # history survives the re-emission

    def test_unknown_supersedes_is_harmless(self):
        with tempfile.TemporaryDirectory() as d:
            merged = orch.merge_decisions(
                d, [{"id": "D-9", "decision": "x", "supersedes": "NOPE"}])
            self.assertEqual(len(merged), 1)


class TestRenderDecisionsLog(unittest.TestCase):
    def test_superseded_hidden_history_compact(self):
        text = orch.render_decisions_log([
            {"id": "D-1", "decision": "SQLite", "superseded": True,
             "superseded_by": "D-2"},
            {"id": "D-2", "decision": "SwiftData", "rationale": "modern",
             "rejected_alternatives": ["SQLite", "CoreData"],
             "constraints": ["iOS 17+"], "supersedes": "D-1"},
        ])
        self.assertNotIn("[D-1] SQLite", text)
        self.assertIn("[D-2] SwiftData", text)
        self.assertIn("rejected: SQLite; CoreData", text)
        self.assertIn("constraints: iOS 17+", text)
        self.assertIn("(supersedes D-1)", text)
        self.assertIn("1 superseded decision(s) hidden", text)

    def test_empty_is_blank(self):
        self.assertEqual(orch.render_decisions_log([]), "")


class TestContractRequest(unittest.TestCase):
    def _phase(self, **kw):
        return wf.Phase("design_discussion", ".", "d.md", "design", **kw)

    def test_discussion_phase_of_app_target_gets_decisions_contract(self):
        cfg = {"_workflow_target": "app"}
        self.assertTrue(orch._decisions_contract_requested(cfg, self._phase()))
        self.assertIn("decisions-json",
                      orch.prompt_coordinate(cfg, "claude", "ctx", self._phase(), 1))

    def test_build_verify_audit_phases_excluded(self):
        cfg = {"_workflow_target": "app"}
        self.assertFalse(orch._decisions_contract_requested(
            cfg, self._phase(writes=True)))
        self.assertFalse(orch._decisions_contract_requested(
            cfg, self._phase(verify={"type": "auto"})))
        self.assertFalse(orch._decisions_contract_requested(
            cfg, self._phase(reads_target=True)))

    def test_non_app_targets_excluded(self):
        self.assertFalse(orch._decisions_contract_requested(
            {"_workflow_target": "audit"}, self._phase()))
        self.assertTrue(orch._decisions_contract_requested(
            {"_workflow_target": "app_spec"}, self._phase()))

    def test_tasks_and_interfaces_instructions_survive(self):
        cfg = {"_workflow_target": "app"}
        ta = wf.Phase("task_assignments", ".", "t.md", "tasks")
        self.assertIn("tasks-json", orch._phase_contract(cfg, ta))
        self.assertIn("decisions-json", orch._phase_contract(cfg, ta))

    def test_record_phase_contracts_persists_decisions(self):
        with tempfile.TemporaryDirectory() as app_dir:
            blob = _block({"decisions": [{"id": "D-1", "decision": "SwiftUI"}]})
            orch._record_phase_contracts({}, "demo", app_dir, "design_discussion",
                                         blob, "", record_decisions=True)
            self.assertEqual([d["id"] for d in orch.load_decisions(app_dir)], ["D-1"])
            # A later phase superseding it marks history rather than deleting.
            blob2 = _block({"id": "D-2", "decision": "UIKit after all",
                            "supersedes": "D-1"})
            orch._record_phase_contracts({}, "demo", app_dir, "tech_review",
                                         blob2, "", record_decisions=True)
            byid = {d["id"]: d for d in orch.load_decisions(app_dir)}
            self.assertTrue(byid["D-1"]["superseded"])


class TestBuildContextInjection(unittest.TestCase):
    def _cfg(self, app_dir, **kw):
        cfg = {"_app_dir": app_dir, "runtime": {},
               "_prior_discussions": "OLD DEBATE TRANSCRIPT"}
        cfg.update(kw)
        return cfg

    def test_decisions_log_injected_before_prior_discussions(self):
        with tempfile.TemporaryDirectory() as app_dir:
            orch.merge_decisions(app_dir, [
                {"id": "D-1", "decision": "old way", "superseded": True},
                {"id": "D-2", "decision": "the new way"}])
            ctx = orch.build_context(self._cfg(app_dir), "demo",
                                     ("phase_x", "f", "f.md", "purpose"),
                                     "prompt", [], "")
        self.assertIn("DECISIONS LOG (structured, authoritative)", ctx)
        self.assertIn("[D-2] the new way", ctx)
        self.assertNotIn("[D-1] old way", ctx)      # superseded entries hidden
        self.assertLess(ctx.index("DECISIONS LOG"),
                        ctx.index("PRIOR PHASE DISCUSSIONS"))

    def test_no_decisions_file_no_block(self):
        with tempfile.TemporaryDirectory() as app_dir:
            ctx = orch.build_context(self._cfg(app_dir), "demo",
                                     ("phase_x", "f", "f.md", "purpose"),
                                     "prompt", [], "")
        self.assertNotIn("DECISIONS LOG", ctx)

    def test_drop_prior_discussions_flag(self):
        with tempfile.TemporaryDirectory() as app_dir:
            cfg = self._cfg(app_dir, _drop_prior_discussions=True)
            ctx = orch.build_context(cfg, "demo", ("phase_x", "f", "f.md", "p"),
                                     "prompt", [], "")
        self.assertNotIn("PRIOR PHASE DISCUSSIONS", ctx)
        self.assertNotIn("OLD DEBATE TRANSCRIPT", ctx)


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git not available")
class TestWorkerContextPolicy(unittest.TestCase):
    """End-to-end through _run_parallel_build: workers lose the raw prior
    transcripts under 'contracts', keep them under 'legacy'; the integrator
    keeps them either way."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        self.md_path = os.path.join(self.app_dir, "build.md")
        with open(self.md_path, "w") as f:
            f.write("")
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig_call = orch.call_agent
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent in ("codex", "claude")
        self.prompts = []

        def fake(cfg, app, phase, rnd, agent, prompt):
            self.prompts.append((str(rnd), prompt))
            if "integrate" in str(rnd):
                return "integrated. CONSENSUS: YES"
            with open(os.path.join(cfg["_build_dir"], "%s.swift" % agent), "w") as fh:
                fh.write("//")
            return "built"
        orch.call_agent = fake

    def tearDown(self):
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_avail

    def _run(self, policy):
        cfg = {"agents": {"codex_enabled": True, "claude_enabled": True,
                          "gemini_enabled": False},
               "runtime": {"build_parallel_workers": 2, "build_cross_review": False,
                           "verify_between_iterations": False,
                           "build_context_policy": policy},
               "_allow_writes": True, "_build_dir": self.build_dir,
               "root": self.app_dir, "_workflow_target": "app",
               "_app_dir": self.app_dir,
               "_prior_discussions": "OLD DEBATE TRANSCRIPT MARKER"}
        phase = wf.Phase("build_coordination", ".", "build.md", "b",
                         rounds=1, writes=True)
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        orch._run_parallel_build(cfg, "demo", self.app_dir, phase, "prompt", [],
                                 state, self.md_path, 1, "", "")
        worker = [p for (rnd, p) in self.prompts if "integrate" not in rnd]
        integ = [p for (rnd, p) in self.prompts if "integrate" in rnd]
        return worker, integ

    def test_contracts_policy_drops_worker_transcripts(self):
        worker, integ = self._run("contracts")
        self.assertTrue(worker and integ)
        self.assertTrue(all("OLD DEBATE TRANSCRIPT MARKER" not in p for p in worker))
        self.assertTrue(all("OLD DEBATE TRANSCRIPT MARKER" in p for p in integ))

    def test_legacy_policy_keeps_worker_transcripts(self):
        worker, _integ = self._run("legacy")
        self.assertTrue(all("OLD DEBATE TRANSCRIPT MARKER" in p for p in worker))


class TestBudgetMarker(unittest.TestCase):
    def test_truncation_adds_marker(self):
        out = orch._budget("a" * 100, 10)
        self.assertTrue(out.startswith("[...earlier context truncated...]\n"))
        self.assertTrue(out.endswith("a" * 10))

    def test_no_truncation_no_marker(self):
        self.assertEqual(orch._budget("short", 10), "short")
        self.assertEqual(orch._budget(None, 10), "")


if __name__ == "__main__":
    unittest.main()
