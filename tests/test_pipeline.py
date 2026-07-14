import json, os, subprocess, tempfile, unittest
import orchestrator as orch
import workflows as wf


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10); return True
    except Exception:
        return False


TASKS_BLOCK = """```tasks-json
{"tasks": [
  {"id": "T-001", "title": "Habit model", "owner_lane": "data_domain",
   "files": ["App/Models/Habit.swift"], "depends_on": [],
   "acceptance_criteria": ["model compiles"], "status": "pending"},
  {"id": "T-002", "title": "Main screen", "owner_lane": "primary_ui",
   "files": ["App/Views/MainView.swift"], "depends_on": ["T-001"],
   "acceptance_criteria": ["renders list"], "status": "pending"}
]}
```"""

INTERFACES_BLOCK = """```interfaces-json
{"interfaces": [
  {"name": "HabitStore", "kind": "protocol", "language": "swift",
   "signature": "protocol HabitStore { func all() -> [Habit] }",
   "owning_lane": "data_domain", "notes": "shared store"}
]}
```"""


@unittest.skipUnless(_has_git(), "git not available")
class TestAppPipelineEndToEnd(unittest.TestCase):
    """Drives _run_app_pipeline over the REAL app_build workflow with a fake
    call_agent (canned outputs incl. the §19/§20 fenced blocks), worktree
    isolation off and verify off — validating phase ordering, prior-output
    handoff, and every persisted artifact (tasks.json, interfaces.json,
    live_log.jsonl, the full docs/ set)."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        with open(os.path.join(self.app_dir, "initial_prompt", "initial_prompt.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("Build a tiny habit tracker.\n")
        with open(os.path.join(self.app_dir, "workflow.txt"), "w", encoding="utf-8") as fh:
            fh.write("app_build\n")
        self.calls = []    # (phase, rnd, agent) in call order
        self.prompts = []  # (phase, rnd, agent, prompt)
        self._orig = orch.call_agent

        def fake(cfg, app, phase, rnd, agent, prompt):
            self.calls.append((phase, str(rnd), agent))
            self.prompts.append((phase, str(rnd), agent, prompt))
            bd = cfg.get("_build_dir")
            if "integrate" in str(rnd):
                with open(os.path.join(bd, "App.swift"), "w", encoding="utf-8") as fh:
                    fh.write("// integrated entry point\n")
                return ("Everything is wired up. CONSENSUS: YES\n"
                        "## Final Output\nApp built. TOKEN_%s." % phase.upper())
            if "wrapping up round" in prompt:  # coordinator wrap-up turn
                blocks = ""
                if phase == "task_assignments":
                    blocks = "\n" + TASKS_BLOCK
                elif phase == "tech_specs":
                    blocks = "\n" + INTERFACES_BLOCK
                return ("We agree. CONSENSUS: YES\n## Final Output\n"
                        "Decision for %s. TOKEN_%s.%s" % (phase, phase.upper(), blocks))
            if bd:  # parallel build worker turn — writes clean lane files
                base = agent.split(":")[0]
                with open(os.path.join(bd, "%s_lane.swift" % base), "w",
                          encoding="utf-8") as fh:
                    fh.write("// clean lane code by %s\n" % base)
                return "built my lane"
            return "discussion input from %s" % agent
        orch.call_agent = fake

        self.cfg = {
            "agents": {"codex_enabled": True, "claude_enabled": True,
                       "gemini_enabled": False},
            "runtime": {"worktree_isolation": False,
                        "verify_build_enabled": False,
                        "build_code_changes_enabled": True,
                        "build_parallel_workers": 2,
                        "default_workflow": "app_build"},
            "ios": {"enforce_signing": False},
        }

    def tearDown(self):
        orch.call_agent = self._orig

    def _run(self):
        orch._run_app_pipeline(self.cfg, "demo", self.app_dir,
                               "Build a tiny habit tracker.")
        return orch.load_state(self.app_dir)

    def test_full_pipeline_artifacts_and_ordering(self):
        workflow = wf.load_workflow("app_build")
        expected_keys = [p.key for p in workflow.phases]
        state = self._run()

        # -- run completed cleanly, every phase done, in workflow order --
        self.assertTrue(state["done"], state.get("error"))
        self.assertEqual(state["completed_phases"], expected_keys)
        first_seen = []
        for phase, _rnd, _agent in self.calls:
            if phase not in first_seen:
                first_seen.append(phase)
        # After every workflow phase, the prompt-adherence gate makes one
        # grading call before the done flag (fake reply has no adherence-json
        # block, so the gate passes-through without blocking).
        self.assertEqual(first_seen, expected_keys + ["adherence_gate"])

        # -- prior_outputs handoff: final_review sees earlier decisions --
        fr_prompts = [p for (ph, _r, _a, p) in self.prompts if ph == "final_review"]
        self.assertTrue(fr_prompts)
        self.assertIn("DECISIONS FROM EARLIER PHASES", fr_prompts[0])
        self.assertIn("PRIOR PHASE DISCUSSIONS", fr_prompts[0])
        self.assertIn("TOKEN_TECH_SPECS", fr_prompts[0])
        self.assertIn("TOKEN_TASK_ASSIGNMENTS", fr_prompts[0])

        # -- tasks.json (§19) parsed from the coordinator's fenced block --
        with open(os.path.join(self.app_dir, "tasks.json"), encoding="utf-8") as fh:
            tasks = json.load(fh)
        self.assertEqual([t["id"] for t in tasks["tasks"]], ["T-001", "T-002"])
        self.assertEqual(tasks["errors"], [])

        # -- interfaces.json (§20) --
        with open(os.path.join(self.app_dir, "interfaces.json"), encoding="utf-8") as fh:
            ifaces = json.load(fh)
        self.assertEqual(ifaces["interfaces"][0]["name"], "HabitStore")

        # -- contracts injected into the parallel build workers' prompts --
        worker_prompts = [p for (_ph, _r, _a, p) in self.prompts
                          if "BUILD ITERATION" in p]
        self.assertTrue(worker_prompts)
        self.assertTrue(any("T-001" in p for p in worker_prompts))
        self.assertTrue(all("HabitStore" in p for p in worker_prompts))

        # -- the build actually landed files --
        self.assertTrue(os.path.exists(
            os.path.join(self.app_dir, "app_build", "App.swift")))

        # -- live_log.jsonl (§21): valid JSONL, required fields, expected kinds --
        with open(os.path.join(self.app_dir, "live_log.jsonl"), encoding="utf-8") as fh:
            entries = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
        self.assertGreater(len(entries), len(expected_keys) * 2)
        kinds = {e["kind"] for e in entries}
        for kind in ("phase_started", "agent_turn_completed", "phase_completed",
                     "tasks_recorded", "interfaces_recorded", "secret_scan"):
            self.assertIn(kind, kinds)
        for e in entries:
            for field in ("ts", "lane", "agent", "kind", "summary"):
                self.assertIn(field, e)

        # -- docs (§24): the full deterministic doc set --
        docs_dir = os.path.join(self.app_dir, "docs")
        for name in ("PRD.md", "TECHNICAL_ARCHITECTURE.md", "QA_REPORT.md",
                     "KNOWN_LIMITATIONS.md", "PROJECT_DOCUMENTATION.md",
                     "LAUNCH_READINESS.md", "phase_outputs.json", "findings.json"):
            self.assertTrue(os.path.exists(os.path.join(docs_dir, name)),
                            "missing docs/%s" % name)
        with open(os.path.join(docs_dir, "PRD.md"), encoding="utf-8") as fh:
            self.assertIn("TOKEN_INITIAL_DISCUSSION", fh.read())
        with open(os.path.join(docs_dir, "TECHNICAL_ARCHITECTURE.md"),
                  encoding="utf-8") as fh:
            self.assertIn("TOKEN_TECH_SPECS", fh.read())

        # -- secret gate (§17/§23): clean build scans clean -> PASS --
        with open(os.path.join(docs_dir, "LAUNCH_READINESS.md"), encoding="utf-8") as fh:
            self.assertIn("Secret scan: PASS (0 hardcoded secrets)", fh.read())
        with open(os.path.join(docs_dir, "findings.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["findings"], [])


if __name__ == "__main__":
    unittest.main()
