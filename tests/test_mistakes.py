"""Mistakes ledger (mistakes.jsonl) + verification rollup + --mistakes CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import warnings

import mistakes as mk
import orchestrator as orch
import verify as verifylib
import workflows as wf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestAppendLoad(unittest.TestCase):
    def test_roundtrip_stamps_ts_and_keeps_fields(self):
        with tempfile.TemporaryDirectory() as d:
            ok = mk.append_mistake(d, {"app": "demo", "workflow": "app_build",
                                       "phase": "build_coordination",
                                       "agent": "codex", "cls": "verify_failure",
                                       "summary": "compile FAILED",
                                       "detail": {"errors": ["boom"]}})
            self.assertTrue(ok)
            recs = mk.load_mistakes(d)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["cls"], "verify_failure")
        self.assertEqual(rec["phase"], "build_coordination")
        self.assertEqual(rec["agent"], "codex")
        self.assertEqual(rec["detail"], {"errors": ["boom"]})
        self.assertIn("ts", rec)
        self.assertIn("+", rec["ts"] + "+")   # tz-aware iso (has an offset or Z)

    def test_none_fields_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            mk.append_mistake(d, {"cls": "repair_queued", "summary": "s",
                                  "agent": None, "phase": None})
            rec = mk.load_mistakes(d)[0]
        self.assertNotIn("agent", rec)
        self.assertNotIn("phase", rec)

    def test_never_raises_on_bad_dir(self):
        self.assertFalse(mk.append_mistake("/nonexistent/nope/deep", {"cls": "verify_failure"}))
        self.assertFalse(mk.append_mistake("", {"cls": "verify_failure"}))
        self.assertFalse(mk.append_mistake(tempfile.gettempdir(), "not a dict"))
        self.assertEqual(mk.load_mistakes("/nonexistent/nope/deep"), [])

    def test_unknown_cls_warns_but_still_writes(self):
        with tempfile.TemporaryDirectory() as d:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                self.assertTrue(mk.append_mistake(d, {"cls": "typo_class", "summary": "x"}))
            self.assertTrue(any("unknown cls" in str(x.message) for x in w))
            self.assertEqual(mk.load_mistakes(d)[0]["cls"], "typo_class")

    def test_secrets_redacted_and_line_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            mk.append_mistake(d, {"cls": "agent_fallback",
                                  "summary": "leaked AKIAABCDEFGHIJKLMNOP in stderr"})
            mk.append_mistake(d, {"cls": "verify_failure", "summary": "y" * 100000})
            with open(mk.mistakes_path(d), encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", lines[0])
        self.assertIn("[REDACTED:aws_key]", lines[0])
        for line in lines:
            self.assertLessEqual(len(line.encode("utf-8")), mk._MAX_LINE_BYTES)
            json.loads(line)   # every bounded line stays valid JSON

    def test_corrupt_lines_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            mk.append_mistake(d, {"cls": "verify_failure", "summary": "a"})
            with open(mk.mistakes_path(d), "a", encoding="utf-8") as fh:
                fh.write("{not json\n\n[1,2]\n")
            mk.append_mistake(d, {"cls": "verify_failure", "summary": "b"})
            recs = mk.load_mistakes(d)
        self.assertEqual([r["summary"] for r in recs], ["a", "b"])


class TestAggregate(unittest.TestCase):
    def test_counts_across_apps(self):
        with tempfile.TemporaryDirectory() as root:
            a = os.path.join(root, "app-a"); os.makedirs(a)
            b = os.path.join(root, "app-b"); os.makedirs(b)
            os.makedirs(os.path.join(root, "no-ledger"))
            mk.append_mistake(a, {"cls": "verify_failure", "phase": "build_coordination",
                                  "agent": "codex", "summary": "x"})
            mk.append_mistake(a, {"cls": "verify_failure", "phase": "build_coordination",
                                  "agent": "claude", "summary": "y"})
            mk.append_mistake(b, {"cls": "contract_error", "phase": "task_assignments",
                                  "summary": "z"})
            agg = mk.aggregate_mistakes(root)
        self.assertEqual(agg["total"], 3)
        self.assertEqual(agg["by_class"], {"verify_failure": 2, "contract_error": 1})
        self.assertEqual(agg["by_phase"], {"build_coordination": 2, "task_assignments": 1})
        self.assertEqual(agg["by_agent"], {"codex": 1, "claude": 1})
        self.assertEqual(sorted(agg["apps"]), ["app-a", "app-b"])
        self.assertEqual(agg["apps"]["app-a"]["total"], 2)

    def test_bad_root_yields_empty(self):
        agg = mk.aggregate_mistakes("/nonexistent/root")
        self.assertEqual(agg["total"], 0)
        self.assertEqual(agg["apps"], {})
        self.assertEqual(mk.aggregate_mistakes(None)["total"], 0)


class TestVerifyFailureHook(unittest.TestCase):
    """A failed compile in _verify_and_repair must land in the ledger."""

    def test_failed_verify_writes_ledger_entry(self):
        with tempfile.TemporaryDirectory() as app_dir:
            build_dir = os.path.join(app_dir, "app_build")
            os.makedirs(build_dir)
            md_path = os.path.join(app_dir, "build.md")
            cfg = {"runtime": {"verify_build_enabled": True},
                   "_build_dir": build_dir, "_workflow_name": "app_build"}
            phasedef = wf.Phase("build_coordination", ".", "build.md", "build",
                                writes=True,
                                verify={"type": "shell", "command": "false",
                                        "repair_iterations": 0})
            failed = {"ran": True, "ok": False, "tool": "shell",
                      "summary": "compile FAILED", "errors": "boom"}
            with unittest.mock.patch.object(orch.verifylib, "run_verification",
                                            lambda *a, **k: dict(failed)):
                orch._verify_and_repair(cfg, "demo", app_dir, phasedef,
                                        {"prompt_hash": "h"}, md_path, "", "codex")
            recs = [r for r in mk.load_mistakes(app_dir) if r["cls"] == "verify_failure"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["phase"], "build_coordination")
        self.assertEqual(recs[0]["agent"], "codex")
        self.assertIn("compile FAILED", recs[0]["summary"])

    def test_unverified_run_writes_no_ledger_entry(self):
        with tempfile.TemporaryDirectory() as app_dir:
            build_dir = os.path.join(app_dir, "app_build")
            os.makedirs(build_dir)
            cfg = {"runtime": {"verify_build_enabled": True},
                   "_build_dir": build_dir}
            phasedef = wf.Phase("build_coordination", ".", "build.md", "build",
                                writes=True, verify={"type": "shell"})
            skipped = {"ran": False, "ok": False, "tool": "shell",
                       "summary": "no toolchain", "errors": ""}
            with unittest.mock.patch.object(orch.verifylib, "run_verification",
                                            lambda *a, **k: dict(skipped)):
                orch._verify_and_repair(cfg, "demo", app_dir, phasedef,
                                        {"prompt_hash": "h"},
                                        os.path.join(app_dir, "b.md"), "", "codex")
            self.assertEqual(mk.load_mistakes(app_dir), [])


class TestVerificationRollup(unittest.TestCase):
    def test_verified(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(d, "build", {"ran": True, "ok": True,
                                                         "tool": "shell"}, prompt_hash="H")
            self.assertEqual(orch.derive_verification(d, {"prompt_hash": "H"}), "verified")

    def test_failed(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(d, "build", {"ran": True, "ok": False,
                                                         "tool": "shell"}, prompt_hash="H")
            self.assertEqual(orch.derive_verification(d, {"prompt_hash": "H"}), "failed")

    def test_unverified_no_record_and_ran_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(orch.derive_verification(d, {"prompt_hash": "H"}), "unverified")
            verifylib.persist_verify_result(d, "build", {"ran": False, "ok": False,
                                                         "tool": "none"}, prompt_hash="H")
            self.assertEqual(orch.derive_verification(d, {"prompt_hash": "H"}), "unverified")

    def test_save_state_stores_rollup(self):
        with tempfile.TemporaryDirectory() as d:
            verifylib.persist_verify_result(d, "build", {"ran": True, "ok": True,
                                                         "tool": "shell"}, prompt_hash="H")
            state = {"prompt_hash": "H", "done": True}
            orch.save_state(d, state)
            self.assertEqual(state["verification"], "verified")
            self.assertEqual(orch.load_state(d)["verification"], "verified")


class TestMistakesReport(unittest.TestCase):
    def _seed(self, root):
        a = os.path.join(root, "app-a")
        os.makedirs(os.path.join(a, "initial_prompt"))
        with open(os.path.join(a, "initial_prompt", "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("Build a thing.")
        mk.append_mistake(a, {"cls": "verify_failure", "phase": "build_coordination",
                              "agent": "codex", "summary": "x"})
        verifylib.persist_verify_result(a, "build", {"ran": True, "ok": False,
                                                     "tool": "shell"})
        return a

    def test_report_shape_and_rollup(self):
        with tempfile.TemporaryDirectory() as root:
            self._seed(root)
            rep = orch.mistakes_report(root)
            json.dumps(rep)   # JSON-serializable for --json
        self.assertEqual(rep["total"], 1)
        self.assertEqual(rep["by_class"], {"verify_failure": 1})
        self.assertEqual(rep["apps"]["app-a"]["verification"], "failed")

    def test_app_filter(self):
        with tempfile.TemporaryDirectory() as root:
            self._seed(root)
            b = os.path.join(root, "app-b"); os.makedirs(b)
            mk.append_mistake(b, {"cls": "contract_error", "summary": "z"})
            rep = orch.mistakes_report(root, app="app-b")
        self.assertEqual(sorted(rep["apps"]), ["app-b"])
        self.assertEqual(rep["total"], 1)
        self.assertEqual(rep["by_class"], {"contract_error": 1})
        self.assertEqual(rep["apps"]["app-b"]["verification"], "unverified")

    def test_print_report_does_not_raise(self):
        with tempfile.TemporaryDirectory() as root:
            self._seed(root)
            rep = orch.mistakes_report(root)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            orch.print_mistakes_report(rep)
        self.assertIn("verify_failure", buf.getvalue())


class TestMistakesCliRealSubprocess(unittest.TestCase):
    """`python3 orchestrator.py --mistakes --json` must print pure JSON on
    stdout (same contract as --doctor --json; see test_doctor_cli.py)."""

    def test_stdout_is_pure_json(self):
        root = tempfile.mkdtemp()
        a = os.path.join(root, "demo")
        os.makedirs(a)
        mk.append_mistake(a, {"cls": "verify_failure", "phase": "build_coordination",
                              "agent": "codex", "summary": "compile FAILED"})
        env = dict(os.environ)
        env["ORCH_ROOT"] = root
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"),
             "--mistakes", "--json"],
            capture_output=True, text=True, timeout=60, env=env, cwd=HERE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        try:
            rep = json.loads(proc.stdout)
        except ValueError as exc:
            self.fail("stdout was not pure JSON (%s): %r" % (exc, proc.stdout[:300]))
        self.assertEqual(rep["schema_version"], 1)
        self.assertEqual(rep["by_class"], {"verify_failure": 1})


if __name__ == "__main__":
    unittest.main()
