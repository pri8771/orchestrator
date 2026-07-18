"""V3 board 4.12: the artifact pre-push gate.

gate.py (deterministic hooks + semantic llm_rules, a stdlib+procutil+schemas
leaf) and its orchestrator wiring (_gate_and_publish) at the single publish
choke point: hooks matrix, stderr→retry-prompt feedback, bounded retry-then-
quarantine, pass-after-feedback, corrupt-config fail-closed, exact retry bound,
quarantine excluded from admission, and the gateless golden path (zero spawns).
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import artifacts as artlib
import events as evlib
import gate as gatelib
import mistakes as mistklib
import orchestrator as orch
import procutil
import schemas


def _script(dir_, name, sh):
    p = os.path.join(dir_, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(sh)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _fenced(type="idea", title="T", body="Some sufficient body text."):
    return ("prose before\n\n```artifact-json\n"
            + json.dumps({"type": type, "title": title, "body": body})
            + "\n```\nprose after\n")


def _mkhook(**raw):
    return gatelib._norm_hook(raw)


# ---------------------------------------------------------------------------
# gate.py units
# ---------------------------------------------------------------------------
class TestGateConfig(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.sec = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.addCleanup(shutil.rmtree, self.sec, True)

    def test_absent_is_empty_inactive_and_silent(self):
        warns = []
        cfg = gatelib.load_gate_config(self.ws, self.sec, on_error=warns.append)
        self.assertEqual(cfg, {"hooks": [], "llm_rules": [], "unreadable": False})
        self.assertFalse(warns)
        self.assertFalse(gatelib.gate_is_active(cfg))

    def test_corrupt_fails_closed_with_banner_and_is_active(self):
        with open(os.path.join(self.ws, "hooks.json"), "w") as fh:
            fh.write("{ not json")
        warns = []
        cfg = gatelib.load_gate_config(self.ws, self.sec, on_error=warns.append)
        self.assertTrue(cfg["unreadable"])
        self.assertTrue(warns and "unreadable" in warns[0])
        self.assertTrue(gatelib.gate_is_active(cfg))

    def test_layers_union_section_and_workspace(self):
        with open(os.path.join(self.sec, "hooks.json"), "w") as fh:
            json.dump({"hooks": [{"id": "s", "command": "true"}]}, fh)
        with open(os.path.join(self.ws, "hooks.json"), "w") as fh:
            json.dump({"llm_rules": [{"id": "w", "rule": "cite a source"}]}, fh)
        cfg = gatelib.load_gate_config(self.ws, self.sec)
        self.assertEqual([h["id"] for h in cfg["hooks"]], ["s"])
        self.assertEqual([r["id"] for r in cfg["llm_rules"]], ["w"])
        self.assertTrue(gatelib.gate_is_active(cfg))

    def test_hook_without_command_is_skipped_loudly(self):
        with open(os.path.join(self.ws, "hooks.json"), "w") as fh:
            json.dump({"hooks": [{"id": "nocmd"}]}, fh)
        warns = []
        cfg = gatelib.load_gate_config(self.ws, self.sec, on_error=warns.append)
        self.assertEqual(cfg["hooks"], [])
        self.assertTrue(any("no runnable" in w for w in warns))


class TestDeterministicHooks(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.allow = _script(self.d, "allow.sh", "#!/bin/sh\nexit 0\n")
        self.block = _script(self.d, "block.sh",
                             "#!/bin/sh\necho 'body too short' >&2\nexit 2\n")
        self.err1 = _script(self.d, "err1.sh", "#!/bin/sh\nexit 1\n")
        self.slow = _script(self.d, "slow.sh", "#!/bin/sh\nsleep 5\n")

    def _cfg(self, hooks):
        return {"hooks": hooks, "llm_rules": [], "unreadable": False}

    def _run(self, hook, type="idea", section="docs", on_error=None):
        return gatelib.run_deterministic_hooks(
            self._cfg([hook]), type, section, "body", {}, cwd=self.d,
            on_error=on_error)

    def test_exit0_allows(self):
        self.assertEqual(self._run(_mkhook(id="a", command=self.allow)),
                         {"blocks": [], "errors": []})

    def test_exit2_blocks_with_stderr(self):
        r = self._run(_mkhook(id="b", command=self.block))
        self.assertEqual(r["errors"], [])
        self.assertEqual(len(r["blocks"]), 1)
        self.assertIn("body too short", r["blocks"][0][1])

    def test_other_exit_is_nonblocking_error(self):
        warns = []
        r = self._run(_mkhook(id="e", command=self.err1), on_error=warns.append)
        self.assertEqual(r["blocks"], [])
        self.assertEqual(r["errors"], [("e", "exit 1")])
        self.assertTrue(warns)

    def test_timeout_is_nonblocking_error(self):
        r = self._run(_mkhook(id="s", command=self.slow, timeout_s=1))
        self.assertEqual(r["blocks"], [])
        self.assertTrue(r["errors"] and "timed out" in r["errors"][0][1])

    def test_missing_command_is_nonblocking_error(self):
        r = self._run(_mkhook(id="m", command=["/no/such/binary"]))
        self.assertEqual(r["blocks"], [])
        self.assertTrue(r["errors"] and "could not run" in r["errors"][0][1])

    def test_embedded_nul_command_never_raises(self):
        # Finding #6: a hooks.json string with a NUL makes Popen raise
        # ValueError (not OSError) — the "Never raises" contract must hold.
        r = self._run(_mkhook(id="n", command=["/bin/echo", "hi\x00there"]))
        self.assertEqual(r["blocks"], [])
        self.assertTrue(r["errors"] and "could not run" in r["errors"][0][1])

    def test_type_scoping(self):
        h = _mkhook(id="b", command=self.block,
                    matcher={"artifact_types": ["gap"]})
        self.assertEqual(self._run(h, type="idea")["blocks"], [])
        self.assertTrue(self._run(h, type="gap")["blocks"])

    def test_section_scoping(self):
        h = _mkhook(id="b", command=self.block,
                    matcher={"sections": ["research"]})
        self.assertEqual(self._run(h, section="docs")["blocks"], [])
        self.assertTrue(self._run(h, section="research")["blocks"])

    def test_stderr_is_redacted_and_capped(self):
        secret = _script(self.d, "secret.sh",
                         "#!/bin/sh\necho 'sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345' "
                         ">&2\nexit 2\n")
        r = self._run(_mkhook(id="x", command=secret))
        fb = r["blocks"][0][1]
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", fb)
        # cap: a huge stderr is truncated to _STDERR_CAP
        big = _script(self.d, "big.sh",
                      "#!/bin/sh\nprintf 'x%.0s' $(seq 1 5000) >&2\nexit 2\n")
        r2 = self._run(_mkhook(id="y", command=big))
        self.assertLessEqual(len(r2["blocks"][0][1]), gatelib._STDERR_CAP)


class TestLlmRules(unittest.TestCase):
    def _cfg(self, rules):
        return {"hooks": [], "llm_rules": [gatelib._norm_rule(r) for r in rules],
                "unreadable": False}

    def test_pass(self):
        c = self._cfg([{"id": "r", "rule": "cite a source"}])
        self.assertTrue(gatelib.evaluate_llm_rules(
            c, "idea", "b", {}, lambda p: "GATE: PASS")["passed"])

    def test_fail_carries_reason(self):
        c = self._cfg([{"id": "r", "rule": "cite a source"}])
        v = gatelib.evaluate_llm_rules(
            c, "idea", "b", {}, lambda p: "GATE: FAIL — no source cited")
        self.assertFalse(v["passed"])
        self.assertIn("no source cited", v["reasons"][0])

    def test_unparseable_fails_closed(self):
        c = self._cfg([{"id": "r", "rule": "x"}])
        self.assertFalse(gatelib.evaluate_llm_rules(
            c, "idea", "b", {}, lambda p: "i am not sure")["passed"])

    def test_runner_crash_fails_closed(self):
        c = self._cfg([{"id": "r", "rule": "x"}])
        def boom(p):
            raise RuntimeError("model down")
        self.assertFalse(gatelib.evaluate_llm_rules(
            c, "idea", "b", {}, boom)["passed"])

    def test_applies_to_scopes_out_and_spends_no_turn(self):
        c = self._cfg([{"id": "r", "rule": "x", "applies_to": ["gap"]}])
        called = []
        r = gatelib.evaluate_llm_rules(
            c, "idea", "b", {}, lambda p: called.append(p) or "GATE: FAIL")
        self.assertTrue(r["passed"])
        self.assertFalse(called)

    def test_gate_block_hook_short_circuits_llm(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        blk = _script(d, "block.sh", "#!/bin/sh\necho no >&2\nexit 2\n")
        cfg = {"hooks": [gatelib._norm_hook({"id": "b", "command": blk})],
               "llm_rules": [gatelib._norm_rule({"id": "r", "rule": "x"})],
               "unreadable": False}
        called = []
        v = gatelib.gate_block(cfg, d, {"type": "idea", "title": "T",
                                        "body": "b"}, "docs",
                               lambda p: called.append(p) or "GATE: PASS")
        self.assertFalse(v["passed"])
        self.assertFalse(called, "a hook block must not spend the llm turn")
        self.assertIn(gatelib.FEEDBACK_HEADER, v["feedback"])


# ---------------------------------------------------------------------------
# orchestrator wiring: _gate_and_publish
# ---------------------------------------------------------------------------
class GatedPublishBase(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        self.app_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.proj, True)
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        self.md = os.path.join(self.app_dir, "phase.md")
        self.reg = artlib.load_registry(orch.HERE)
        self.gate_repair_prompts = []
        # Patch the agent-selection + agent-call seams so no real model runs.
        self._orig = {
            "pick_quality_evaluator": orch.pick_quality_evaluator,
            "_coordinator_candidates": orch._coordinator_candidates,
            "call_agent": orch.call_agent,
        }
        orch.pick_quality_evaluator = lambda cfg, a, c: "claude"
        orch._coordinator_candidates = \
            lambda cfg, a, preferred=None, require_healthy=False: ["claude"]
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._orig.items():
            setattr(orch, k, v)

    def set_agent(self, verdicts, reemit_body="Re-emitted longer body text."):
        """call_agent stub: 'gate' turns pop from `verdicts`; 'gate-repair'
        turns record the prompt and return a fresh artifact-json block."""
        vs = list(verdicts)
        def fake(cfg, app, phase, rnd, agent, prompt):
            if rnd == "gate":
                return vs.pop(0) if vs else "GATE: FAIL — exhausted"
            if rnd == "gate-repair":
                self.gate_repair_prompts.append(prompt)
                return _fenced(body=reemit_body)
            return ""
        orch.call_agent = fake

    def _build_seen(self):
        # Mirror _hook_artifact_publish's disk scan so tests exercise the real
        # resume dedupe (triples for clean publishes, source_hash for gated).
        seen, seen_src = set(), set()
        sess = os.path.basename(self.app_dir)
        for m in artlib.list_artifacts(self.proj):
            s = m.get("source") or {}
            if s.get("session") == sess and s.get("phase") == "doc":
                seen.add((m.get("type"), m.get("title"), m.get("content_hash")))
                g = m.get("gate")
                if isinstance(g, dict) and g.get("source_hash"):
                    seen_src.add(g["source_hash"])
        return seen, seen_src

    def run_gate(self, final_output, gate_cfg, cfg=None):
        cfg = cfg or {"root": self.proj, "_app_dir": self.app_dir,
                      "_workflow_name": "documentation"}
        src = {"section": "documentation",
               "session": os.path.basename(self.app_dir), "phase": "doc",
               "turn": ""}
        seen, seen_src = self._build_seen()   # rebuilt each call = resume-faithful
        return orch._gate_and_publish(
            cfg, "App", self.app_dir, "doc", "claude", {"claude": {}},
            self.md, "", self.proj, "documentation", final_output, src,
            self.reg, gate_cfg, seen, seen_src, False)

    def _quarantined(self):
        return [m for m in artlib.list_artifacts(self.proj)
                if m.get("status") == "quarantined"]

    def _published(self):
        return [m for m in artlib.list_artifacts(self.proj)
                if m.get("status") != "quarantined"]

    def _events(self, kind):
        p = os.path.join(self.app_dir, "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh
                    if l.strip() and json.loads(l).get("kind") == kind]

    def _mistakes(self, cls):
        p = os.path.join(self.app_dir, "mistakes.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh
                    if l.strip() and json.loads(l).get("cls") == cls]


class TestGatedPublish(GatedPublishBase):
    def _rule_cfg(self):
        return {"hooks": [], "unreadable": False,
                "llm_rules": [gatelib._norm_rule({"id": "r", "rule": "cite a "
                                                  "source"})]}

    def test_llm_retry_then_quarantine(self):
        self.set_agent(["GATE: FAIL — no source", "GATE: FAIL — still no source"])
        self.run_gate(_fenced(), self._rule_cfg())   # gate_retry_limit default 1
        q = self._quarantined()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["gate"]["attempts"], 2)
        self.assertTrue(q[0]["gate"]["failures"])
        self.assertEqual(len(self.gate_repair_prompts), 1)   # exactly one retry
        self.assertEqual(len(self._events("artifact_quarantined")), 1)
        self.assertEqual(len(self._mistakes("gate_blocked")), 1)
        self.assertEqual(self._events("artifact_published"), [])

    def test_pass_after_feedback_is_not_quarantined(self):
        self.set_agent(["GATE: FAIL — fix it", "GATE: PASS"])
        self.run_gate(_fenced(), self._rule_cfg())
        self.assertEqual(self._quarantined(), [])
        pub = self._published()
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["gate"]["attempts"], 2)   # passed on attempt 2
        self.assertIn("source_hash", pub[0]["gate"])
        self.assertEqual(len(self._events("artifact_published")), 1)
        self.assertEqual(self._events("artifact_quarantined"), [])

    def test_clean_pass_records_no_gate_meta(self):
        self.set_agent(["GATE: PASS"])
        self.run_gate(_fenced(), self._rule_cfg())
        pub = self._published()
        self.assertEqual(len(pub), 1)
        self.assertNotIn("gate", pub[0], "a first-pass clean publish is "
                         "byte-identical — no gate meta")

    def test_retry_limit_is_exact(self):
        for limit in (0, 1, 2):
            with self.subTest(limit=limit):
                # Fresh dirs per iteration WITHOUT re-running setUp (that would
                # re-save the already-patched seams and leak them to later
                # suites) — just reset the state the assertions read.
                self.proj = tempfile.mkdtemp()
                self.app_dir = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, self.proj, True)
                self.addCleanup(shutil.rmtree, self.app_dir, True)
                self.md = os.path.join(self.app_dir, "phase.md")
                self.gate_repair_prompts = []
                self.set_agent(["GATE: FAIL — no"] * (limit + 3))
                cfg = {"root": self.proj, "_app_dir": self.app_dir,
                       "_workflow_name": "documentation",
                       "runtime": {"gate_retry_limit": limit}}
                self.run_gate(_fenced(), self._rule_cfg(), cfg=cfg)
                q = self._quarantined()
                self.assertEqual(len(q), 1)
                self.assertEqual(len(self.gate_repair_prompts), limit)
                self.assertEqual(q[0]["gate"]["attempts"], limit + 1)

    def test_deterministic_hook_stderr_reaches_retry_prompt(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        blk = _script(d, "block.sh",
                      "#!/bin/sh\necho 'MARKER-need-a-citation' >&2\nexit 2\n")
        cfg = {"hooks": [gatelib._norm_hook({"id": "b", "command": blk})],
               "llm_rules": [], "unreadable": False}
        self.set_agent([])   # llm never consulted (hook short-circuits)
        self.run_gate(_fenced(), cfg)
        self.assertEqual(len(self.gate_repair_prompts), 1)
        self.assertIn("MARKER-need-a-citation", self.gate_repair_prompts[0])
        self.assertIn(gatelib.FEEDBACK_HEADER, self.gate_repair_prompts[0])
        self.assertEqual(len(self._quarantined()), 1)

    def test_corrupt_config_fails_closed_without_spawning(self):
        cfg = {"hooks": [], "llm_rules": [], "unreadable": True}
        self.set_agent([])
        orig = procutil.run_capture
        procutil.run_capture = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("run_capture must not be called on unreadable config"))
        self.addCleanup(setattr, procutil, "run_capture", orig)
        self.run_gate(_fenced(), cfg)
        q = self._quarantined()
        self.assertEqual(len(q), 1)
        self.assertTrue(any("unreadable" in f
                            for f in q[0]["gate"]["failures"]))
        self.assertEqual(self.gate_repair_prompts, [],
                         "no retry on an unreadable config")

    def test_crash_resume_does_not_duplicate_quarantine(self):
        # A quarantined artifact recorded its source_hash; a re-close (seen/
        # seen_src rebuilt from disk) must skip it — no -2 duplicate, no fresh
        # repair turn.
        self.set_agent(["GATE: FAIL — no"] * 6)
        self.run_gate(_fenced(), self._rule_cfg())
        first = len(self._quarantined())
        n_prompts = len(self.gate_repair_prompts)
        self.run_gate(_fenced(), self._rule_cfg())   # re-close
        self.assertEqual(len(self._quarantined()), first,
                         "re-close must not duplicate the quarantined artifact")
        self.assertEqual(len(self.gate_repair_prompts), n_prompts,
                         "re-close must not re-run the repair turn")

    def test_crash_resume_does_not_duplicate_repaired_pass(self):
        # Finding #2: a repaired-then-passed artifact stores content_hash of the
        # REPAIRED body but source_hash of the ORIGINAL; the re-close re-extracts
        # the ORIGINAL block, which must be matched via seen_src (source_hash).
        self.set_agent(["GATE: FAIL — fix", "GATE: PASS", "GATE: PASS"])
        self.run_gate(_fenced(), self._rule_cfg())
        self.assertEqual(len(self._published()), 1)
        n_prompts = len(self.gate_repair_prompts)
        self.run_gate(_fenced(), self._rule_cfg())   # re-close
        self.assertEqual(len(self._published()), 1,
                         "re-close must not duplicate the repaired artifact")
        self.assertEqual(len(self.gate_repair_prompts), n_prompts)

    def test_two_blocks_same_body_different_title_both_publish(self):
        # Findings #1/#5: two distinct blocks that merely SHARE a body must both
        # publish (gateless parity) — the body-hash-alone dedupe collapsed them.
        self.set_agent([])   # hooks-only, no llm
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        allow = _script(d, "allow.sh", "#!/bin/sh\nexit 0\n")
        cfg = {"hooks": [gatelib._norm_hook({"id": "a", "command": allow})],
               "llm_rules": [], "unreadable": False}
        out = (_fenced(type="idea", title="Cache layer", body="Add caching")
               + _fenced(type="idea", title="No cache", body="Add caching"))
        self.run_gate(out, cfg)
        titles = sorted(m["title"] for m in self._published())
        self.assertEqual(titles, ["Cache layer", "No cache"],
                         "both distinct blocks with a shared body must publish")

    def test_repaired_nonstring_body_quarantines_not_drops(self):
        # Finding #3: a re-emit with a non-string body must NOT silently drop the
        # artifact — quarantine still lands (on the last publishable block).
        def fake(cfg, app, phase, rnd, agent, prompt):
            if rnd == "gate":
                return "GATE: FAIL — no"
            if rnd == "gate-repair":
                self.gate_repair_prompts.append(prompt)
                return ('```artifact-json\n'
                        + json.dumps({"type": "idea", "title": "T", "body": 42})
                        + '\n```\n')          # non-string body
            return ""
        orch.call_agent = fake
        self.run_gate(_fenced(), self._rule_cfg())
        q = self._quarantined()
        self.assertEqual(len(q), 1, "a bad re-emit must not silently drop it")
        self.assertTrue(any("non-string body" in f
                            for f in q[0]["gate"]["failures"]))

    def test_hook_error_records_a_mistake(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        errhook = _script(d, "err.sh", "#!/bin/sh\nexit 3\n")   # non-0/2 = error
        cfg = {"hooks": [gatelib._norm_hook({"id": "broken", "command": errhook})],
               "llm_rules": [], "unreadable": False}
        self.set_agent([])
        self.run_gate(_fenced(), cfg)
        # exit 3 is non-blocking → the artifact publishes …
        self.assertEqual(len(self._published()), 1)
        # … but the broken hook is recorded in the ledger (finding #4).
        self.assertEqual(len(self._mistakes("gate_hook_error")), 1)


class TestQuarantineExclusion(unittest.TestCase):
    def test_quarantined_is_inadmissible_but_written(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj, True)
        reg = artlib.load_registry(orch.HERE)
        aid = artlib.publish(proj, "# X\n\nbody", {"type": "idea", "title": "T"},
                             reg, gate={"source_hash": "h", "attempts": 2,
                                        "failures": ["blocked"]})
        meta = artlib.load_meta(proj, aid)
        self.assertEqual(meta["status"], "quarantined")
        self.assertFalse(artlib.is_admissible(proj, meta))   # never routes/retrieves
        self.assertTrue(artlib.is_routable(meta))            # not converged
        self.assertTrue(os.path.exists(os.path.join(
            artlib.artifacts_root(proj), aid, "body.md")))   # on disk, inspectable


class TestGatelessGolden(unittest.TestCase):
    def test_inactive_gate_spawns_nothing(self):
        cfg = {"hooks": [], "llm_rules": [], "unreadable": False}
        self.assertFalse(gatelib.gate_is_active(cfg))
        orig = procutil.run_capture
        procutil.run_capture = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no hook → run_capture must never be called"))
        self.addCleanup(setattr, procutil, "run_capture", orig)
        r = gatelib.run_deterministic_hooks(cfg, "idea", "docs", "b", {})
        self.assertEqual(r, {"blocks": [], "errors": []})


class TestRegistriesAndPurity(unittest.TestCase):
    def test_artifact_quarantined_registered(self):
        self.assertIn("artifact_quarantined", evlib.KINDS)

    def test_gate_blocked_registered(self):
        self.assertIn("gate_blocked", mistklib.CLASSES)

    def test_gate_module_is_a_clean_leaf(self):
        # gate.py may import stdlib + procutil + schemas ONLY (no orchestrator,
        # no artifacts) — the same leaf discipline artifacts.py holds.
        with open(gatelib.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("import orchestrator", "import artifacts",
                       "import events", "import sessions"):
            self.assertNotIn(banned, src, banned + " leaked into gate.py")


if __name__ == "__main__":
    unittest.main()
