"""Tests for the tasks-json/interfaces-json contract repair loop
(_record_phase_contracts / _repair_contract): a malformed or incomplete
machine-contract block used to just log a WARN CONTRACT line and let the
build proceed on a broken contract. It now gets bounded, targeted repair
turns first (NEXT_MILESTONES.md item 1)."""
import json
import os
import tempfile
import unittest

import orchestrator as orch


def _tasks_block(*tasks):
    return "```tasks-json\n" + json.dumps({"tasks": list(tasks)}) + "\n```"


def _interfaces_block(*ifaces):
    return "```interfaces-json\n" + json.dumps({"interfaces": list(ifaces)}) + "\n```"


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


BAD_TASK = {"id": "T-1", "title": "Do the thing", "owner_lane": "bogus_lane",
           "files": ["Foo.swift"], "status": "pending"}
GOOD_TASK = {"id": "T-1", "title": "Do the thing", "owner_lane": "primary_ui",
            "files": ["Foo.swift"], "status": "pending"}

BAD_IFACE = {"name": "Widget", "kind": "struct", "owning_lane": "primary_ui"}   # missing "language"
GOOD_IFACE = {"name": "Widget", "kind": "struct", "language": "Swift",
             "owning_lane": "primary_ui"}


class _RepairTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_call_agent = orch.call_agent
        self.tmp = tempfile.mkdtemp()
        self.app_dir = self.tmp
        self.md_path = os.path.join(self.tmp, "phase.md")
        open(self.md_path, "w").close()

    def tearDown(self):
        orch.call_agent = self._orig_call_agent

    def _cfg(self, limit=2):
        return {"runtime": {"contract_repair_limit": limit}}


class TestTaskContractRepair(_RepairTestBase):
    def test_fixes_within_limit_and_records_no_mistake(self):
        calls = []

        def fake_call_agent(cfg, app, phase, rnd, agent, prompt):
            calls.append((phase, rnd, agent))
            return _tasks_block(GOOD_TASK)

        orch.call_agent = fake_call_agent
        transcript = orch._record_phase_contracts(
            self._cfg(), "demo", self.app_dir, "task_assignments",
            _tasks_block(BAD_TASK), "",
            coord="codex", active=["codex"], md_path=self.md_path)

        tasks = _read_json(os.path.join(self.app_dir, "tasks.json"))
        self.assertEqual(tasks["errors"], [])
        self.assertEqual(tasks["tasks"][0]["owner_lane"], "primary_ui")
        self.assertEqual(len(calls), 1)   # one repair turn, then it's clean
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, "mistakes.jsonl")))
        # The repair exchange is durably recorded in the phase transcript.
        self.assertIn("tasks-json repair", _read_text(self.md_path))
        self.assertIn("tasks-json repair", transcript)

    def test_exhausts_limit_then_warns_and_persists_errors(self):
        def fake_call_agent(cfg, app, phase, rnd, agent, prompt):
            return _tasks_block(BAD_TASK)   # never actually fixes it

        orch.call_agent = fake_call_agent
        orch._record_phase_contracts(
            self._cfg(limit=2), "demo", self.app_dir, "task_assignments",
            _tasks_block(BAD_TASK), "",
            coord="codex", active=["codex"], md_path=self.md_path)

        tasks = _read_json(os.path.join(self.app_dir, "tasks.json"))
        self.assertEqual(len(tasks["errors"]), 1)
        mistakes = _read_text(os.path.join(self.app_dir, "mistakes.jsonl"))
        self.assertIn("contract_error", mistakes)
        self.assertIn("2 repair attempt(s)", mistakes)

    def test_no_coord_active_or_md_path_skips_repair_like_before(self):
        # Matches the pre-existing call convention (see
        # test_decisions_contract.py) — repair must stay opt-in via the new
        # keyword args, not change behavior for existing call sites.
        called = []
        orch.call_agent = lambda *a, **k: called.append(1) or "unused"
        orch._record_phase_contracts(
            self._cfg(), "demo", self.app_dir, "task_assignments",
            _tasks_block(BAD_TASK), "")
        self.assertEqual(called, [])
        tasks = _read_json(os.path.join(self.app_dir, "tasks.json"))
        self.assertEqual(len(tasks["errors"]), 1)
        self.assertIn("contract_error", _read_text(os.path.join(self.app_dir, "mistakes.jsonl")))

    def test_limit_zero_disables_repair(self):
        called = []
        orch.call_agent = lambda *a, **k: called.append(1) or "unused"
        orch._record_phase_contracts(
            self._cfg(limit=0), "demo", self.app_dir, "task_assignments",
            _tasks_block(BAD_TASK), "",
            coord="codex", active=["codex"], md_path=self.md_path)
        self.assertEqual(called, [])


class TestInterfaceContractRepair(_RepairTestBase):
    def test_fixes_within_limit(self):
        def fake_call_agent(cfg, app, phase, rnd, agent, prompt):
            return _interfaces_block(GOOD_IFACE)

        orch.call_agent = fake_call_agent
        orch._record_phase_contracts(
            self._cfg(), "demo", self.app_dir, "tech_specs",
            _interfaces_block(BAD_IFACE), "",
            coord="claude", active=["claude"], md_path=self.md_path)

        ifaces = _read_json(os.path.join(self.app_dir, "interfaces.json"))
        self.assertEqual(ifaces["errors"], [])
        self.assertEqual(ifaces["interfaces"][0]["language"], "Swift")

    def test_exhausts_and_warns(self):
        orch.call_agent = lambda *a, **k: _interfaces_block(BAD_IFACE)
        orch._record_phase_contracts(
            self._cfg(limit=1), "demo", self.app_dir, "tech_specs",
            _interfaces_block(BAD_IFACE), "",
            coord="claude", active=["claude"], md_path=self.md_path)

        ifaces = _read_json(os.path.join(self.app_dir, "interfaces.json"))
        self.assertEqual(len(ifaces["errors"]), 1)
        self.assertIn("1 repair attempt(s)",
                      _read_text(os.path.join(self.app_dir, "mistakes.jsonl")))


class TestUnrelatedPhaseUnaffected(_RepairTestBase):
    def test_non_contract_phase_ignores_new_kwargs(self):
        # design_discussion isn't task_assignments/tech_specs — coord/active/
        # md_path must be harmless no-ops there (existing behavior preserved).
        called = []
        orch.call_agent = lambda *a, **k: called.append(1) or "unused"
        transcript = orch._record_phase_contracts(
            self._cfg(), "demo", self.app_dir, "design_discussion",
            "some transcript", "final output",
            coord="codex", active=["codex"], md_path=self.md_path)
        self.assertEqual(called, [])
        self.assertIn("some transcript", transcript)


if __name__ == "__main__":
    unittest.main()
