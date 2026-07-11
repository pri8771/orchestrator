import os, subprocess, tempfile, unittest
import orchestrator as orch
import workflows as wf


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10); return True
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git not available")
class TestWorktreeBuildEndToEnd(unittest.TestCase):
    """Exercises the real _run_parallel_build worktree path with a fake agent, so
    the merge/isolation wiring is validated without any live CLI or tokens."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        with open(os.path.join(self.app_dir, "build_coordination.md"), "w") as f:
            f.write("")
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig = orch.call_agent
        self._orig_available = orch._agent_available
        orch._agent_available = lambda agent: agent in ("codex", "claude")

        # Fake agent: each worker writes a DISTINCT file into its own build dir
        # (its worktree in isolation mode); the integrator writes a shared file.
        def fake(cfg, app, phase, rnd, agent, prompt):
            bd = cfg.get("_build_dir")
            if "integrate" in str(rnd):
                path = os.path.join(bd, "App.swift")
                with open(path, "w") as fh: fh.write("// integrated\nCONSENSUS: YES\n")
                return "integrated. CONSENSUS: YES"
            base = agent.split(":")[0]
            with open(os.path.join(bd, "%s_lane.swift" % base), "w") as fh:
                fh.write("// by %s\n" % base)
            return "wrote %s lane" % base
        orch.call_agent = fake

    def tearDown(self):
        orch.call_agent = self._orig
        orch._agent_available = self._orig_available

    def _cfg(self, worktree):
        return {
            "agents": {"codex_enabled": True, "claude_enabled": True, "gemini_enabled": False},
            "runtime": {"worktree_isolation": worktree, "build_parallel_workers": 2},
            "_allow_writes": True, "_build_dir": self.build_dir, "root": self.app_dir,
            "_workflow_target": "app", "_workflow_name": "vslice",
        }

    def _phase(self):
        return wf.Phase("build_coordination", ".", "build_coordination.md", "build it",
                        rounds=1, writes=True)

    def _run(self, worktree):
        cfg = self._cfg(worktree)
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        consensus, out, _tr = orch._run_parallel_build(
            cfg, "demo", self.app_dir, self._phase(), "build a tiny app", [],
            state, os.path.join(self.app_dir, "build_coordination.md"),
            1, "", "")
        return consensus

    def test_worktree_mode_merges_all_lanes(self):
        self._run(worktree=True)
        files = os.listdir(self.build_dir)
        # both isolated lanes merged back into app_build
        self.assertIn("codex_lane.swift", files)
        self.assertIn("claude_lane.swift", files)
        # and it's a git repo with a committed history
        code, out, _ = orch._git(self.build_dir, "log", "--oneline")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())

    def test_direct_mode_still_works(self):
        # worktree off -> the proven direct-write path; files land directly.
        self._run(worktree=False)
        files = os.listdir(self.build_dir)
        self.assertIn("codex_lane.swift", files)
        self.assertIn("claude_lane.swift", files)

    def test_integrator_failover_uses_next_healthy_coordinator(self):
        def fake(cfg, app, phase, rnd, agent, prompt):
            bd = cfg.get("_build_dir")
            if "integrate" in str(rnd):
                if agent == "claude":
                    raise orch.AgentError("Claude skipped — in cooldown (timeout).")
                with open(os.path.join(bd, "App.swift"), "w") as fh:
                    fh.write("// integrated by %s\nCONSENSUS: YES\n" % agent)
                return "integrated by %s. CONSENSUS: YES" % agent
            with open(os.path.join(bd, "%s_lane.swift" % agent), "w") as fh:
                fh.write("// by %s\n" % agent)
            return "wrote %s lane" % agent

        orch.call_agent = fake
        self._run(worktree=False)
        with open(os.path.join(self.build_dir, "App.swift"), encoding="utf-8") as fh:
            self.assertIn("integrated by codex", fh.read())

    def test_repeated_missing_integrator_aborts_build_loop(self):
        def fake(cfg, app, phase, rnd, agent, prompt):
            bd = cfg.get("_build_dir")
            if "integrate" in str(rnd):
                raise orch.AgentError("%s unavailable" % agent)
            with open(os.path.join(bd, "%s_lane_%s.swift" % (agent, rnd)), "w") as fh:
                fh.write("// by %s\n" % agent)
            return "wrote %s lane" % agent

        orch.call_agent = fake
        cfg = self._cfg(worktree=False)
        cfg["runtime"]["max_build_iterations_without_integrator"] = 1
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        with self.assertRaises(orch.AgentError) as ctx:
            orch._run_parallel_build(
                cfg, "demo", self.app_dir, self._phase(), "build a tiny app", [],
                state, os.path.join(self.app_dir, "build_coordination.md"),
                3, "", "")
        self.assertIn("Observer stopped build_coordination", str(ctx.exception))

    def test_missing_workers_stall_then_abort_gracefully(self):
        def fake(cfg, app, phase, rnd, agent, prompt):
            if "integrate" in str(rnd):
                return "integrator fallback %s" % agent
            raise orch.AgentError("%s unavailable" % agent)

        orch.call_agent = fake
        cfg = self._cfg(worktree=False)
        cfg["runtime"]["max_build_iterations_without_integrator"] = 2
        state = {"prompt_hash": "h", "phase_outputs": {}, "consensus_status": {},
                 "completed_phases": [], "current_round": 0}
        with self.assertRaises(orch.AgentError) as ctx:
            orch._run_parallel_build(
                cfg, "demo", self.app_dir, self._phase(), "build a tiny app", [],
                state, os.path.join(self.app_dir, "build_coordination.md"),
                3, "", "")
        self.assertIn("no build worker output", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
