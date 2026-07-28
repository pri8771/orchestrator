"""The 17-task batch's load-bearing new logic: design/dependency lint,
fleet learning (incidents/ratings/presort/exemplars), Definition of Done
tiers, the requirements-json contract, vertical-slice wave layering, rolling
summary preference, and the eval-harness scorer."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

import completeness as complib
import designlint as dlint
import evalharness as evallib
import fleetlearn as fllib
import orchestrator as orch
import verify as verifylib
import workflows as wf


def _has_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cget(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class TestDesignLint(unittest.TestCase):
    def setUp(self):
        self.build = tempfile.mkdtemp()
        self.here = tempfile.mkdtemp()

    def _w(self, rel, text):
        p = os.path.join(self.build, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_inline_color_and_font_are_errors_outside_ds(self):
        self._w("Views/Home.swift",
                'let c = Color(red: 0.1, green: 0.2, blue: 0.3)\n'
                'Text("x").font(.system(size: 17))\n')
        self._w("DesignSystem.swift",
                'let accent = Color(red: 1, green: 0, blue: 0)\n')
        errors, _w = dlint.scan(self.build, self.here)
        rules = sorted(e["rule"] for e in errors)
        self.assertEqual(rules, ["inline_color", "raw_font_size"])
        self.assertTrue(all(e["file"] != "DesignSystem.swift" for e in errors))

    def test_missing_design_system_and_todo_warn(self):
        for n in ("A", "B", "C"):
            self._w(n + ".swift", "// TODO: finish\nstruct %s {}\n" % n)
        errors, warnings = dlint.scan(self.build, self.here)
        self.assertEqual(errors, [])
        rules = {w["rule"] for w in warnings}
        self.assertIn("missing_design_system", rules)
        self.assertIn("todo_marker", rules)

    def test_banned_and_unlisted_packages(self):
        with open(os.path.join(self.here, "tech_stack.json"), "w") as fh:
            json.dump({"allowed": [{"name": "swift-collections"}],
                       "banned": [{"name": "Alamofire", "why": "no"}]}, fh)
        self._w("Package.swift",
                'dependencies: [\n'
                ' .package(url: "https://github.com/Alamofire/Alamofire.git", from: "5.0.0"),\n'
                ' .package(url: "https://github.com/apple/swift-algorithms", from: "1.0.0"),\n'
                ']\n')
        errors, warnings = dlint.scan(self.build, self.here)
        self.assertEqual([e["rule"] for e in errors], ["banned_package"])
        self.assertIn("unlisted_package", {w["rule"] for w in warnings})

    def test_gate_fails_on_errors_passes_clean(self):
        app_dir = tempfile.mkdtemp()
        bd = os.path.join(app_dir, "app_build")
        os.makedirs(bd)
        with open(os.path.join(bd, "Home.swift"), "w") as fh:
            fh.write("let c = Color(red: 1, green: 1, blue: 1)\n")
        cfg = {"runtime": {"design_lint_enabled": True}}
        reason = dlint.run_design_lint(cfg, _cget, lambda m: None, "x",
                                       app_dir, self.here)
        self.assertIn("inline_color", reason)
        saved = json.load(open(os.path.join(app_dir, "docs",
                                            "design_lint.json")))
        self.assertEqual(len(saved["errors"]), 1)
        os.remove(os.path.join(bd, "Home.swift"))
        with open(os.path.join(bd, "Clean.swift"), "w") as fh:
            fh.write("struct Clean {}\n")
        self.assertIsNone(dlint.run_design_lint(cfg, _cget, lambda m: None,
                                                "x", app_dir, self.here))


class TestFleetLearn(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()

    def test_incident_attribution_defaults(self):
        e = fllib.record_incident(self.app_dir, "visual_qa", "blank screen")
        self.assertIn("design_handoff", e["blamed_phases"])
        loaded = fllib.load_incidents(self.app_dir)
        self.assertEqual(loaded[0]["gate"], "visual_qa")

    def test_rating_roundtrip_and_clear(self):
        self.assertTrue(fllib.save_rating(self.app_dir, "bad", "ugly nav"))
        self.assertEqual(fllib.load_rating(self.app_dir)["verdict"], "bad")
        self.assertTrue(fllib.save_rating(self.app_dir, None))
        self.assertIsNone(fllib.load_rating(self.app_dir))

    def test_presort_ranks_failures_first(self):
        root = tempfile.mkdtemp()
        for name, state in (("clean", {"done": True}),
                            ("broken", {"done": False, "error": "boom",
                                        "release_gate_repairs": 2})):
            d = os.path.join(root, name, "initial_prompt")
            os.makedirs(d)
            open(os.path.join(d, "initial_prompt.md"), "w").write("p")
            with open(os.path.join(root, name, "agent_state.json"), "w") as fh:
                json.dump(state, fh)
        rows = fllib.presort(root)
        self.assertEqual(rows[0]["app"], "broken")
        self.assertEqual(rows[0]["proposed"], "bad")
        self.assertEqual(rows[-1]["proposed"], "good")

    def test_export_exemplars(self):
        here = tempfile.mkdtemp()
        with open(os.path.join(self.app_dir, "agent_state.json"), "w") as fh:
            json.dump({"phase_outputs": {
                "design_handoff": "x" * 300, "app_features": "y" * 300,
                "tiny": "z"}}, fh)
        written = fllib.export_exemplars(self.app_dir, here, "demo")
        names = {os.path.basename(os.path.dirname(w)) for w in written}
        self.assertEqual(names, {"design_handoff", "app_features"})

    def test_load_phase_exemplar_is_empty_when_none_exported(self):
        old_here = orch.HERE
        orch.HERE = tempfile.mkdtemp()
        try:
            self.assertEqual(orch._load_phase_exemplar("app_features"), "")
        finally:
            orch.HERE = old_here

    def test_load_phase_exemplar_reads_back_what_export_writes(self):
        # The actual --save-exemplar -> phase-prompt round trip: write via
        # fleetlearn.export_exemplars, read back via
        # orchestrator._load_phase_exemplar, same `here` root both sides.
        here = tempfile.mkdtemp()
        with open(os.path.join(self.app_dir, "agent_state.json"), "w") as fh:
            json.dump({"phase_outputs": {
                "app_features": "A really good feature list." * 20}}, fh)
        fllib.export_exemplars(self.app_dir, here, "demo")
        old_here = orch.HERE
        orch.HERE = here
        try:
            out = orch._load_phase_exemplar("app_features")
        finally:
            orch.HERE = old_here
        self.assertIn("EXEMPLAR", out)
        self.assertIn("A really good feature list.", out)

    def test_load_phase_exemplar_picks_newest_file(self):
        here = tempfile.mkdtemp()
        d = os.path.join(here, "knowledge", "exemplars", "app_features")
        os.makedirs(d)
        old_path = os.path.join(d, "old.md")
        new_path = os.path.join(d, "new.md")
        with open(old_path, "w") as fh:
            fh.write("OLD EXEMPLAR")
        with open(new_path, "w") as fh:
            fh.write("NEW EXEMPLAR")
        os.utime(old_path, (1000, 1000))
        os.utime(new_path, (2000, 2000))
        old_here = orch.HERE
        orch.HERE = here
        try:
            out = orch._load_phase_exemplar("app_features")
        finally:
            orch.HERE = old_here
        self.assertIn("NEW EXEMPLAR", out)
        self.assertNotIn("OLD EXEMPLAR", out)

    def test_ledger_written_from_incidents(self):
        root = tempfile.mkdtemp()
        here = tempfile.mkdtemp()
        d = os.path.join(root, "p1", "initial_prompt")
        os.makedirs(d)
        open(os.path.join(d, "initial_prompt.md"), "w").write("p")
        fllib.record_incident(os.path.join(root, "p1"), "design_lint",
                              "inline colors in 3 files")
        path, clusters = fllib.build_ledger(root, here, model="")
        self.assertTrue(path.endswith("anti_patterns.md"))
        body = open(path).read()
        self.assertIn("design_lint", body)
        self.assertEqual(clusters, 1)

    def test_read_ledger_roundtrips_what_build_writes(self):
        # The READ half of fleet learning: what build_ledger records must
        # come back for prompt injection (it was write-only — no code path
        # ever put the ledger in front of an agent).
        root = tempfile.mkdtemp()
        here = tempfile.mkdtemp()
        d = os.path.join(root, "p1", "initial_prompt")
        os.makedirs(d)
        open(os.path.join(d, "initial_prompt.md"), "w").write("p")
        fllib.record_incident(os.path.join(root, "p1"), "visual_qa",
                              "blank screen shipped")
        fllib.build_ledger(root, here, model="")
        text = fllib.read_ledger(here)
        self.assertIn("visual_qa", text)
        self.assertIn("blank screen shipped", text)

    def test_read_ledger_absent_is_empty_and_never_raises(self):
        self.assertEqual(fllib.read_ledger(tempfile.mkdtemp()), "")

    def test_read_ledger_caps_size(self):
        here = tempfile.mkdtemp()
        os.makedirs(os.path.join(here, "knowledge"))
        with open(os.path.join(here, "knowledge", "anti_patterns.md"),
                  "w") as fh:
            fh.write("x" * 10000)
        self.assertEqual(len(fllib.read_ledger(here)), 2000)

    def test_read_ledger_redirects_engine_here_like_build(self):
        # Same ORCH_LEDGER_DIR contract as build_ledger: pointing at the real
        # engine checkout must NOT read the repo's tracked ledger in tests.
        import fleetlearn
        engine_here = os.path.dirname(os.path.abspath(fleetlearn.__file__))
        sandbox = os.environ["ORCH_LEDGER_DIR"]
        os.makedirs(os.path.join(sandbox, "knowledge"), exist_ok=True)
        with open(os.path.join(sandbox, "knowledge", "anti_patterns.md"),
                  "w") as fh:
            fh.write("sandboxed ledger content")
        self.assertEqual(fllib.read_ledger(engine_here),
                         "sandboxed ledger content")


@unittest.skipUnless(_has_git(), "git not available")
class TestDeadWorkerClaimRevert(unittest.TestCase):
    """A-26: a worker whose CLI is installed (so it stays in the roster) but
    whose every call fails keeps its sticky claims forever — the build
    'proceeds' while its slice is silently never built. After 2 consecutive
    failed iterations the claims must revert to the open pool AND land with a
    LIVE worker (not bounce back to the dead lane via lane preference)."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        self.md_path = os.path.join(self.app_dir, "build_coordination.md")
        open(self.md_path, "w").close()
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig_call = orch.call_agent
        self._orig_avail = orch._agent_available
        orch._agent_available = \
            lambda agent, cfg=None: agent in ("codex", "claude")
        self.prompts = []   # (rnd, agent, prompt)

        def fake(cfg, app, phase, rnd, agent, prompt):
            self.prompts.append((str(rnd), agent, prompt))
            if "integrate" in str(rnd):
                it = int(str(rnd).split(".")[0])
                return "integrated. CONSENSUS: %s" % ("YES" if it >= 3 else "NO")
            if agent == "claude":
                raise orch.AgentError("claude CLI logged out")
            with open(os.path.join(cfg.get("_build_dir"),
                                   "codex_%s.swift" % str(rnd).replace(".", "_")),
                      "w", encoding="utf-8") as fh:
                fh.write("// ok\n")
            return "wrote my lane"
        orch.call_agent = fake

        def _task(tid, lane):
            return {"id": tid, "title": tid, "owner_lane": lane, "files": [],
                    "depends_on": [], "acceptance_criteria": [],
                    "status": "pending"}
        # < 4 would disable claiming's wave machinery differently; 4 tasks,
        # no deps -> single wave -> vertical slices stay OFF (waves == []).
        orch.persist_tasks(self.app_dir, [
            _task("T-data", "data_domain"),
            _task("T-ui", "primary_ui"),
            _task("T-svc", "services_utilities"),
            _task("T-polish", "polish_resilience"),
        ], [])

    def tearDown(self):
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_avail

    def test_failing_workers_claims_revert_and_reach_live_workers(self):
        cfg = {"agents": {"codex_enabled": True, "claude_enabled": True,
                          "gemini_enabled": False},
               "runtime": {"worktree_isolation": False,
                           "build_parallel_workers": 2,
                           "build_cross_review": False,
                           "verify_between_iterations": False},
               "_allow_writes": True, "_build_dir": self.build_dir,
               "root": self.app_dir, "_workflow_target": "app"}
        phase = wf.Phase("build_coordination", ".", "build_coordination.md",
                         "build it", rounds=1, writes=True)
        state = {"prompt_hash": "h", "phase_outputs": {},
                 "consensus_status": {}, "completed_phases": [],
                 "current_round": 0}
        consensus, _out, _tr = orch._run_parallel_build(
            cfg, "demo", self.app_dir, phase, "build", [], state,
            self.md_path, 4, "", "")
        self.assertTrue(consensus)
        claimed = {t["id"]: t.get("claimed_by")
                   for t in orch.load_tasks(self.app_dir)}
        # Claude failed iterations 1 and 2 -> two-strike revert -> its tasks
        # re-claimed by live codex workers on iteration 3.
        for tid, cb in claimed.items():
            self.assertIsNotNone(cb, "%s ended unclaimed" % tid)
            self.assertTrue(str(cb).startswith("codex"),
                            "%s still claimed by dead worker %s" % (tid, cb))
        # And a live worker's iteration-3 prompt actually carries the
        # redistributed tasks (redistribution reached a builder, not just
        # tasks.json).
        it3_codex = " ".join(p for (rnd, a, p) in self.prompts
                             if rnd.startswith("3.") and a == "codex"
                             and "integrate" not in rnd)
        self.assertIn("T-ui", it3_codex)
        self.assertIn("T-polish", it3_codex)


class TestTaskErrorsSurviveBuild(unittest.TestCase):
    """A-72: _record_phase_contracts WARNs the user to 'review tasks.json
    errors', but the build loop's claim persistence rewrote the file with
    errors=[] on its first iteration — destroying the record the user was
    pointed at. The claim persistence must carry the recorded errors through."""

    ERRORS = ["task block 2: missing required field 'files'"]

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        self.md_path = os.path.join(self.app_dir, "build_coordination.md")
        open(self.md_path, "w").close()
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig_call = orch.call_agent
        self._orig_avail = orch._agent_available
        self.addCleanup(self._restore)
        orch._agent_available = lambda agent, cfg=None: agent == "codex"

        def fake(cfg, app, phase, rnd, agent, prompt):
            if "integrate" in str(rnd):
                return "integrated. CONSENSUS: YES"
            return "wrote my lane"
        orch.call_agent = fake
        orch.persist_tasks(self.app_dir, [
            {"id": "T-1", "title": "T-1", "owner_lane": "primary_ui",
             "files": [], "depends_on": [], "acceptance_criteria": [],
             "status": "pending"}], self.ERRORS)

    def _restore(self):
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_avail

    def test_contract_errors_survive_the_claim_persistence(self):
        self.assertEqual(orch.load_task_errors(self.app_dir), self.ERRORS)
        cfg = {"agents": {"codex_enabled": True, "claude_enabled": False,
                          "gemini_enabled": False},
               "runtime": {"worktree_isolation": False,
                           "build_cross_review": False,
                           "verify_between_iterations": False},
               "_allow_writes": True, "_build_dir": self.build_dir,
               "root": self.app_dir, "_workflow_target": "app"}
        phase = wf.Phase("build_coordination", ".", "build_coordination.md",
                         "build it", rounds=1, writes=True)
        state = {"prompt_hash": "h", "phase_outputs": {},
                 "consensus_status": {}, "completed_phases": [],
                 "current_round": 0}
        orch._run_parallel_build(cfg, "demo", self.app_dir, phase, "build",
                                 [], state, self.md_path, 1, "", "")
        with open(os.path.join(self.app_dir, "tasks.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data.get("errors"), self.ERRORS,
                         "the claim persistence must not blank the record")


@unittest.skipUnless(_has_git(), "git not available")
class TestFinalIterationWaveMerge(unittest.TestCase):
    """A-27: with more dependency waves than build iterations, the trailing
    waves were never scheduled (min() only clamps iterations beyond the wave
    count) — contradicting the in-code contract that they 'collapse into the
    final iteration'. The last budgeted iteration must work ALL remaining
    waves."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        self.md_path = os.path.join(self.app_dir, "build_coordination.md")
        open(self.md_path, "w").close()
        self.build_dir = os.path.join(self.app_dir, "app_build")
        os.makedirs(self.build_dir)
        self._orig_call = orch.call_agent
        self._orig_avail = orch._agent_available
        orch._agent_available = \
            lambda agent, cfg=None: agent in ("codex", "claude")
        self.prompts = []   # (rnd, agent, prompt)

        def fake(cfg, app, phase, rnd, agent, prompt):
            self.prompts.append((str(rnd), agent, prompt))
            if "integrate" in str(rnd):
                it = int(str(rnd).split(".")[0])
                return "integrated. CONSENSUS: %s" % ("YES" if it >= 2 else "NO")
            with open(os.path.join(cfg.get("_build_dir"),
                                   "%s_%s.swift" % (agent, str(rnd).replace(".", "_"))),
                      "w", encoding="utf-8") as fh:
                fh.write("// ok\n")
            return "wrote my lane"
        orch.call_agent = fake

        # A 6-task dependency chain -> 6 waves of one task each; vertical
        # slices activate (backlog >= 4, > 1 wave, default-on knob).
        tasks = []
        for i in range(6):
            tasks.append({"id": "T-%d" % i, "title": "t%d" % i,
                          "owner_lane": "primary_ui", "files": [],
                          "depends_on": (["T-%d" % (i - 1)] if i else []),
                          "acceptance_criteria": [], "status": "pending"})
        orch.persist_tasks(self.app_dir, tasks, [])

    def tearDown(self):
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_avail

    def test_budget_short_of_waves_merges_remainder_into_final_iteration(self):
        cfg = {"agents": {"codex_enabled": True, "claude_enabled": True,
                          "gemini_enabled": False},
               "runtime": {"worktree_isolation": False,
                           "build_parallel_workers": 2,
                           "build_cross_review": False,
                           "verify_between_iterations": False},
               "_allow_writes": True, "_build_dir": self.build_dir,
               "root": self.app_dir, "_workflow_target": "app"}
        phase = wf.Phase("build_coordination", ".", "build_coordination.md",
                         "build it", rounds=1, writes=True)
        state = {"prompt_hash": "h", "phase_outputs": {},
                 "consensus_status": {}, "completed_phases": [],
                 "current_round": 0}
        consensus, _out, _tr = orch._run_parallel_build(
            cfg, "demo", self.app_dir, phase, "build", [], state,
            self.md_path, 2, "", "")
        self.assertTrue(consensus)
        # Every task was scheduled: waves W2-W6 collapsed into iteration 2
        # instead of T-2..T-5 staying claimed_by=None forever.
        claimed = {t["id"]: t.get("claimed_by")
                   for t in orch.load_tasks(self.app_dir)}
        for tid, cb in claimed.items():
            self.assertIsNotNone(cb, "%s was never scheduled" % tid)
        # The final iteration's worker prompts carry the merged remainder and
        # say so (workers must not be told 'this wave ONLY' while holding
        # five waves).
        it2 = " ".join(p for (rnd, a, p) in self.prompts
                       if rnd.startswith("2.") and "integrate" not in rnd)
        self.assertIn("T-5", it2)
        self.assertIn("ALL remaining waves W2-W6", it2)


class TestLedgerRefreshRoot(unittest.TestCase):
    """A-30: the post-run ledger refresh must aggregate from cfg['root'] (the
    workspace root), not dirname(app_dir) — for a nested session
    <root>/<project>/<section>/<chat>, dirname is the SECTION dir, and
    build_ledger would rewrite the fleet-wide knowledge/anti_patterns.md from
    that one section's chats, discarding every other project's clusters."""

    def test_pipeline_rebuilds_ledger_from_workspace_root(self):
        root = tempfile.mkdtemp()
        sid = "proj/ideas/chat1"
        app_dir = os.path.join(root, sid)
        os.makedirs(os.path.join(app_dir, "initial_prompt"))
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("test\n")
        with open(os.path.join(app_dir, "workflow.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("answer_question\n")

        def phase(_cfg, _app, app_dir_, phasedef, _prompt, _prior, state,
                  phase_index=0):
            del phase_index
            state["current_phase"] = phasedef.key
            state.setdefault("completed_phases", []).append(phasedef.key)
            state.setdefault("phase_outputs", {})[phasedef.key] = "ok"
            orch.save_state(app_dir_, state)
            return "ok"

        seen = []

        def fake_build_ledger(*args, **_kwargs):
            seen.append(args[0])
            return "", 0

        cfg = {"root": root,
               "runtime": {"fleet_ledger_enabled": True,
                           "fetch_prompt_urls": False},
               "agents": {"codex_enabled": True, "claude_enabled": False,
                          "gemini_enabled": False},
               "models": {}, "ios": {}}
        with unittest.mock.patch.object(orch, "process_phase",
                                        side_effect=phase), \
                unittest.mock.patch.object(orch, "_release_gate_failure",
                                           return_value=None), \
                unittest.mock.patch.object(orch, "_adherence_gate",
                                           return_value=None), \
                unittest.mock.patch.object(orch.fllib, "build_ledger",
                                           side_effect=fake_build_ledger):
            orch._run_app_pipeline(cfg, sid, app_dir, "test")
        self.assertTrue(orch.load_state(app_dir)["done"])
        self.assertEqual(seen, [root])


class TestDefinitionOfDone(unittest.TestCase):
    def test_tiers_inherit(self):
        proto = complib.dod_items(HERE, "prototype")
        v1 = complib.dod_items(HERE, "v1")
        self.assertTrue(set(proto).issubset(set(v1)))
        self.assertGreater(len(v1), len(proto))

    def test_render_and_unknown_tier(self):
        block = complib.render_dod(HERE, "nonsense")
        self.assertIn("DEFINITION OF DONE", block)
        self.assertIn("tier: v1", block)

    def test_file_override(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "definition_of_done.json"), "w") as fh:
            json.dump({"prototype": ["only this"]}, fh)
        self.assertEqual(complib.dod_items(d, "prototype"), ["only this"])


class TestRequirementsContract(unittest.TestCase):
    def test_parse_and_dedupe(self):
        text = ("```requirements-json\n"
                + json.dumps({"requirements": [
                    {"id": "R-001", "text": "add habits", "core": True},
                    {"id": "R-002", "text": "streak view"},
                    {"id": "R-001", "text": "add habits (revised)"},
                    {"text": "no id"}]})
                + "\n```")
        reqs, errors = orch.parse_requirements_blocks(text)
        byid = {r["id"]: r for r in reqs}
        self.assertEqual(len(reqs), 2)
        self.assertIn("revised", byid["R-001"]["text"])   # last emission wins
        self.assertTrue(byid["R-002"]["core"])            # core defaults True
        self.assertTrue(errors)

    def test_persist_load_roundtrip(self):
        app_dir = tempfile.mkdtemp()
        orch.persist_requirements(app_dir, [{"id": "R-001", "text": "t",
                                             "core": True}])
        self.assertEqual(orch.load_requirements(app_dir)[0]["id"], "R-001")


class TestVerticalWaves(unittest.TestCase):
    def _t(self, tid, deps=()):
        return {"id": tid, "title": tid, "owner_lane": "primary_ui",
                "depends_on": list(deps)}

    def test_topo_layers(self):
        backlog = [self._t("T-1"), self._t("T-2"),
                   self._t("T-3", ["T-1"]), self._t("T-4", ["T-2", "T-3"])]
        waves = orch._task_waves(backlog)
        ids = [[t["id"] for t in w] for w in waves]
        self.assertEqual(ids, [["T-1", "T-2"], ["T-3"], ["T-4"]])

    def test_small_backlogs_skip_slicing(self):
        self.assertEqual(orch._task_waves([self._t("T-1"), self._t("T-2")]), [])

    def test_cycle_becomes_final_wave(self):
        backlog = [self._t("T-1"), self._t("T-2"),
                   self._t("T-3", ["T-4"]), self._t("T-4", ["T-3"])]
        waves = orch._task_waves(backlog)
        self.assertEqual([t["id"] for t in waves[-1]], ["T-3", "T-4"])

    def test_unknown_deps_count_satisfied(self):
        backlog = [self._t("T-%d" % i, ["GHOST"]) for i in range(1, 5)]
        waves = orch._task_waves(backlog)
        self.assertEqual(waves, [])   # one wave only -> no slicing


class TestRollingSummaryPreference(unittest.TestCase):
    def test_summary_replaces_transcript(self):
        app_dir = tempfile.mkdtemp()
        phase = ("alpha", "alpha", "alpha.md", "p")
        d = os.path.join(app_dir, "alpha")
        os.makedirs(d)
        open(os.path.join(d, "alpha.md"), "w").write("RAW TRANSCRIPT TEXT")
        phases = [phase]
        full = orch.prior_discussion_context(app_dir, phases, ["alpha"])
        self.assertIn("RAW TRANSCRIPT", full)
        summed = orch.prior_discussion_context(
            app_dir, phases, ["alpha"], summaries={"alpha": "THE SUMMARY"})
        self.assertIn("THE SUMMARY", summed)
        self.assertNotIn("RAW TRANSCRIPT", summed)


class TestEvalHarness(unittest.TestCase):
    def test_score_project_composite(self):
        app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(app_dir, "docs"))
        # verify.py's real on-disk shape is a bare LIST of records (written by
        # persist_verify_result), not {"attempts": [...]}. Use the real writer
        # so this test can't drift from production again.
        verifylib.persist_verify_result(app_dir, "release_gate",
                                        {"ran": True, "ok": True})
        json.dump({"verdict": "PASS", "score": 80},
                  open(os.path.join(app_dir, "docs", "adherence.json"), "w"))
        json.dump({"verdict": "PASS", "score": 100},
                  open(os.path.join(app_dir, "docs", "visual_qa.json"), "w"))
        json.dump({"flows": [{"passed": True}, {"passed": True}],
                   "crashes": [], "dead_taps": []},
                  open(os.path.join(app_dir, "docs", "ui_crawl.json"), "w"))
        json.dump({"errors": [], "warnings": []},
                  open(os.path.join(app_dir, "docs", "design_lint.json"), "w"))
        json.dump({"done": True},
                  open(os.path.join(app_dir, "agent_state.json"), "w"))
        r = evallib.score_project(app_dir)
        # 30 compile + 20 adherence + 15 visual + 15 flows + 10 crawl + 5 lint
        self.assertEqual(r["composite"], 95)
        self.assertTrue(r["compile_ok"])
        self.assertEqual(r["flows"], "2/2")

    def test_missing_evidence_is_not_scored_as_zero_defects(self):
        # A project with NO crawl/lint reports at all (gates never ran) must
        # not collect the crawl-hygiene/lint points that a genuinely clean
        # pass would earn — missing evidence != zero defects.
        app_dir = tempfile.mkdtemp()
        json.dump({"done": True},
                  open(os.path.join(app_dir, "agent_state.json"), "w"))
        r = evallib.score_project(app_dir)
        self.assertEqual(r["composite"], 0)
        self.assertFalse(r["compile_ok"])
        self.assertEqual(r["crashes"], "n/a")
        self.assertEqual(r["dead_buttons"], "n/a")
        self.assertEqual(r["lint_errors"], "n/a")


if __name__ == "__main__":
    unittest.main()


class TestBuildLaneLocals(unittest.TestCase):
    def _cfg(self, allow_locals):
        return {"runtime": {"build_parallel_workers": 3,
                            "locals_in_build_lanes": allow_locals}}

    def test_locals_excluded_from_lanes_by_default(self):
        real = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True
        try:
            roster = orch.build_worker_roster(
                self._cfg(False),
                ["codex", "claude", "local:qwen3-coder:30b"])
            agents = [w["agent"] for w in roster]
            self.assertNotIn("local:qwen3-coder:30b", agents)
            self.assertIn("codex", agents)
            self.assertIn("claude", agents)
        finally:
            orch._agent_available = real

    def test_opt_in_keeps_locals_in_lanes(self):
        real = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True
        try:
            roster = orch.build_worker_roster(
                self._cfg(True),
                ["codex", "claude", "local:qwen3-coder:30b"])
            self.assertIn("local:qwen3-coder:30b",
                          [w["agent"] for w in roster])
        finally:
            orch._agent_available = real

    def test_all_local_roster_still_builds(self):
        """Cloud-only filter must never strand a local-only setup."""
        real = orch._agent_available
        orch._agent_available = lambda a, cfg=None: True
        try:
            roster = orch.build_worker_roster(
                self._cfg(False), ["local:qwen2.5-coder:14b"])
            self.assertTrue(roster)
            self.assertEqual(roster[0]["agent"], "local:qwen2.5-coder:14b")
        finally:
            orch._agent_available = real
