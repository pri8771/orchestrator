import json, os, tempfile, unittest
import orchestrator as orch


def _task(tid, lane="data_domain", deps=None):
    return {"id": tid, "title": "task %s" % tid, "owner_lane": lane,
            "files": ["App/%s.swift" % tid], "depends_on": deps or [],
            "acceptance_criteria": ["compiles"], "status": "pending"}


class TestTasksJsonParsing(unittest.TestCase):
    def test_wrapper_object_parses_all_tasks(self):
        text = "prose\n```tasks-json\n%s\n```" % json.dumps(
            {"tasks": [_task("T-001"), _task("T-002", "primary_ui", ["T-001"])]})
        tasks, errors = orch.parse_tasks_blocks(text)
        self.assertEqual([t["id"] for t in tasks], ["T-001", "T-002"])
        self.assertEqual(errors, [])

    def test_one_task_per_block(self):
        text = ("```tasks-json\n%s\n```\nchatter\n```tasks-json\n%s\n```"
                % (json.dumps(_task("T-001")), json.dumps(_task("T-002"))))
        tasks, errors = orch.parse_tasks_blocks(text)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(errors, [])

    def test_malformed_block_recorded_not_crash(self):
        text = "```tasks-json\n{not json at all}\n```"
        tasks, errors = orch.parse_tasks_blocks(text)
        self.assertEqual(tasks, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("JSON parse", errors[0])

    def test_missing_required_field_dropped_with_error(self):
        bad = {"id": "T-009", "title": "no lane", "files": [], "status": "pending"}
        text = "```tasks-json\n%s\n```" % json.dumps({"tasks": [_task("T-001"), bad]})
        tasks, errors = orch.parse_tasks_blocks(text)
        self.assertEqual([t["id"] for t in tasks], ["T-001"])
        self.assertEqual(len(errors), 1)
        self.assertIn("owner_lane", errors[0])

    def test_status_normalized_against_vocabulary(self):
        odd = _task("T-010")
        odd["status"] = "DONE "
        text = "```tasks-json\n%s\n```" % json.dumps(odd)
        tasks, errors = orch.parse_tasks_blocks(text)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(errors, [])

        bogus = _task("T-011")
        bogus["status"] = "made-up-status"
        text = "```tasks-json\n%s\n```" % json.dumps(bogus)
        tasks, errors = orch.parse_tasks_blocks(text)
        self.assertEqual(tasks[0]["status"], "pending")

    def test_duplicate_id_last_emission_wins(self):
        first = _task("T-001"); first["title"] = "draft"
        second = _task("T-001"); second["title"] = "final revision"
        text = ("```tasks-json\n%s\n```\n```tasks-json\n%s\n```"
                % (json.dumps(first), json.dumps(second)))
        tasks, _ = orch.parse_tasks_blocks(text)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "final revision")

    def test_defaults_filled(self):
        t = {"id": "T-001", "title": "t", "owner_lane": "primary_ui",
             "files": [], "status": "pending"}
        tasks, errors = orch.parse_tasks_blocks("```tasks-json\n%s\n```" % json.dumps(t))
        self.assertEqual(errors, [])
        self.assertEqual(tasks[0]["depends_on"], [])
        self.assertEqual(tasks[0]["acceptance_criteria"], [])


class TestTaskCycles(unittest.TestCase):
    def test_acyclic_graph_has_no_cycles(self):
        tasks = [_task("T-001"), _task("T-002", deps=["T-001"]),
                 _task("T-003", deps=["T-001", "T-002"])]
        self.assertEqual(orch.find_task_cycles(tasks), [])

    def test_two_node_cycle_detected(self):
        tasks = [_task("T-001", deps=["T-002"]), _task("T-002", deps=["T-001"])]
        cycles = orch.find_task_cycles(tasks)
        self.assertEqual(len(cycles), 1)
        self.assertIn("T-001", cycles[0])
        self.assertIn("T-002", cycles[0])

    def test_self_dependency_detected(self):
        cycles = orch.find_task_cycles([_task("T-001", deps=["T-001"])])
        self.assertEqual(cycles, ["T-001 -> T-001"])

    def test_unknown_dependency_is_not_a_cycle(self):
        self.assertEqual(orch.find_task_cycles([_task("T-001", deps=["T-999"])]), [])


class TestTasksPersistence(unittest.TestCase):
    def test_persist_and_load_round_trip(self):
        app_dir = tempfile.mkdtemp()
        orch.persist_tasks(app_dir, [_task("T-001")], ["dependency cycle: x"])
        path = os.path.join(app_dir, "tasks.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["errors"], ["dependency cycle: x"])
        self.assertEqual(orch.load_tasks(app_dir)[0]["id"], "T-001")

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(orch.load_tasks(tempfile.mkdtemp()), [])


def _iface(name="HabitStore"):
    return {"name": name, "kind": "protocol", "language": "swift",
            "signature": "protocol %s {}" % name, "owning_lane": "data_domain",
            "notes": "shared store"}


class TestInterfacesJsonParsing(unittest.TestCase):
    def test_wrapper_and_single_item_blocks(self):
        text = ("```interfaces-json\n%s\n```\n```interfaces-json\n%s\n```"
                % (json.dumps({"interfaces": [_iface("A"), _iface("B")]}),
                   json.dumps(_iface("C"))))
        items, errors = orch.parse_interface_blocks(text)
        self.assertEqual(sorted(i["name"] for i in items), ["A", "B", "C"])
        self.assertEqual(errors, [])

    def test_missing_required_field_dropped_with_error(self):
        bad = {"name": "X", "kind": "struct", "language": "swift"}  # no owning_lane
        text = "```interfaces-json\n%s\n```" % json.dumps({"interfaces": [bad]})
        items, errors = orch.parse_interface_blocks(text)
        self.assertEqual(items, [])
        self.assertIn("owning_lane", errors[0])

    def test_malformed_block_recorded(self):
        items, errors = orch.parse_interface_blocks(
            '```interfaces-json\n{"oops": }\n```')
        self.assertEqual(items, [])
        self.assertEqual(len(errors), 1)

    def test_persist_and_load_round_trip(self):
        app_dir = tempfile.mkdtemp()
        orch.persist_interfaces(app_dir, [_iface()], [])
        loaded = orch.load_interfaces(app_dir)
        self.assertEqual(loaded[0]["name"], "HabitStore")


class TestWorkerContractBlock(unittest.TestCase):
    """_worker_contract_block just RENDERS whatever it's given — task
    selection is _claim_tasks_for_iteration's job now (see
    TestClaimTasksForIteration below)."""

    def test_claimed_tasks_and_interfaces_both_render(self):
        block = orch._worker_contract_block(
            [_task("T-001")], [_task("T-001")], [_iface()])
        self.assertIn("T-001", block)
        self.assertIn("HabitStore", block)

    def test_backlog_present_but_nothing_claimed_says_so(self):
        block = orch._worker_contract_block([_task("T-001")], [], [])
        self.assertIn("nothing claimed for you", block)

    def test_empty_contract_is_empty_string(self):
        self.assertEqual(orch._worker_contract_block([], [], []), "")


def _roster(*lane_ids, agent="codex"):
    return [{"agent": agent, "label": "%s %d" % (agent, i), "slug": "%s-%d" % (agent, i),
            "lane": lid, "lane_id": lid} for i, lid in enumerate(lane_ids)]


class TestClaimTasksForIteration(unittest.TestCase):
    """§19 dynamic task claiming — replaces the old static
    owner_lane == lane_id slice with claimed_by/claimed_at + redistribution
    of anything a lane-matched worker can't take."""

    def test_own_lane_match_claims_to_that_worker(self):
        roster = _roster("data_domain", "primary_ui")
        backlog = [_task("T-001", "data_domain"), _task("T-002", "primary_ui")]
        claims = orch._claim_tasks_for_iteration(roster, backlog, 1)
        self.assertEqual([t["id"] for t in claims["codex-0"]], ["T-001"])
        self.assertEqual([t["id"] for t in claims["codex-1"]], ["T-002"])
        self.assertEqual(backlog[0]["claimed_by"], "codex-0")
        self.assertEqual(backlog[0]["claimed_at"], 1)

    def test_unknown_lane_label_is_redistributed_not_dropped(self):
        roster = _roster("data_domain", "primary_ui")
        backlog = [_task("T-001", "made_up_lane"), _task("T-002", "another_lane")]
        claims = orch._claim_tasks_for_iteration(roster, backlog, 1)
        claimed_ids = {t["id"] for v in claims.values() for t in v}
        self.assertEqual(claimed_ids, {"T-001", "T-002"})
        self.assertTrue(all(t.get("claimed_by") for t in backlog))

    def test_multiple_workers_sharing_a_lane_split_not_duplicate(self):
        # Two workers both own data_domain (e.g. roster bigger than 4 real
        # lanes) — 3 tasks should split across them, never both claiming #1.
        roster = _roster("data_domain", "data_domain")
        backlog = [_task("T-%03d" % i, "data_domain") for i in range(1, 4)]
        claims = orch._claim_tasks_for_iteration(roster, backlog, 1)
        all_claimed = [t["id"] for v in claims.values() for t in v]
        self.assertEqual(sorted(all_claimed), ["T-001", "T-002", "T-003"])
        self.assertTrue(claims["codex-0"])
        self.assertTrue(claims["codex-1"])

    def test_lane_with_no_worker_this_iteration_overflows_to_roster(self):
        # Only data_domain has a worker; a primary_ui task must still get
        # claimed by someone rather than left to sit unbuilt.
        roster = _roster("data_domain")
        backlog = [_task("T-001", "primary_ui")]
        claims = orch._claim_tasks_for_iteration(roster, backlog, 1)
        self.assertEqual([t["id"] for t in claims["codex-0"]], ["T-001"])

    def test_claims_are_sticky_across_iterations(self):
        roster = _roster("data_domain", "primary_ui")
        backlog = [_task("T-001", "data_domain")]
        orch._claim_tasks_for_iteration(roster, backlog, 1)
        self.assertEqual(backlog[0]["claimed_by"], "codex-0")
        # Second call, same roster: the existing claim must not move even
        # though nothing marks the task "done" between iterations.
        claims2 = orch._claim_tasks_for_iteration(roster, backlog, 2)
        self.assertEqual(backlog[0]["claimed_at"], 1)   # unchanged
        self.assertEqual([t["id"] for t in claims2["codex-0"]], ["T-001"])

    def test_done_tasks_are_never_reclaimed(self):
        roster = _roster("data_domain")
        backlog = [_task("T-001", "data_domain")]
        backlog[0]["status"] = "done"
        claims = orch._claim_tasks_for_iteration(roster, backlog, 1)
        self.assertEqual(claims["codex-0"], [])
        self.assertIsNone(backlog[0].get("claimed_by"))

    def test_stale_claim_reverts_when_worker_leaves_roster(self):
        backlog = [_task("T-001", "data_domain")]
        backlog[0]["claimed_by"] = "codex-0"
        backlog[0]["claimed_at"] = 1
        # codex-0 is gone this iteration — its claim must revert and be
        # picked up by whoever's actually here now.
        roster = _roster("data_domain")   # slug is "codex-0" too (same helper)
        roster[0]["slug"] = "claude-0"
        claims = orch._claim_tasks_for_iteration(roster, backlog, 2)
        self.assertEqual(backlog[0]["claimed_by"], "claude-0")
        self.assertEqual(backlog[0]["claimed_at"], 2)
        self.assertEqual([t["id"] for t in claims["claude-0"]], ["T-001"])


class TestPromptInstructions(unittest.TestCase):
    def test_phase_extra_carries_contracts(self):
        cfg = {"_workflow_target": "app"}
        self.assertIn("tasks-json", orch.phase_extra(cfg, "task_assignments"))
        self.assertIn("interfaces-json", orch.phase_extra(cfg, "tech_specs"))

    def test_coordinator_prompt_carries_contracts(self):
        import workflows as wf
        cfg = {"_workflow_target": "app"}
        ta = wf.Phase("task_assignments", "task_assignments", "t.md", "assign")
        ts = wf.Phase("tech_specs", "tech_specs", "t.md", "spec")
        self.assertIn("tasks-json", orch.prompt_coordinate(cfg, "claude", "ctx", ta, 1))
        self.assertIn("interfaces-json", orch.prompt_coordinate(cfg, "claude", "ctx", ts, 1))


if __name__ == "__main__":
    unittest.main()
