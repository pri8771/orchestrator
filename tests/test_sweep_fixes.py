"""Regression tests for the audit-sweep fixes (findings ledger ORCH-C0xx):

- read_target_path skips comment/blank/invalid lines (C028)
- global worker cap never over-releases after a failed claim (C030)
- per-worker circuit-breaker health keys (C031)
- prune_logs retention + orchestrator.log rotation (C034)
- run_state derived spec-§6 status enum (C038)
- --app/--project/--resume slug validation (C082)
- write_call_log persists the command REDACTED (C103)
- roles/knowledge smoke coverage (C092)
"""
import json
import os
import shutil
import tempfile
import time
import unittest

import orchestrator as orch
import workflows as wflib
import roles as roleslib
import knowledge as knowlib


class TestReadTargetPathSkipsCommentsAndInvalid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app_dir = os.path.join(self.tmp, "app")
        self.target = os.path.join(self.tmp, "the-target")
        os.makedirs(self.app_dir)
        os.makedirs(self.target)
        # read_target_path realpaths its result (/var -> /private/var on macOS).
        self.target = os.path.realpath(self.target)

    def _write_target_file(self, text):
        with open(os.path.join(self.app_dir, "target_path.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)

    def test_commented_first_line_does_not_abort(self):
        self._write_target_file("# old target, kept for reference\n%s\n" % self.target)
        self.assertEqual(wflib.read_target_path(self.app_dir), self.target)

    def test_invalid_line_is_skipped_not_fatal(self):
        self._write_target_file("/definitely/not/a/dir\n%s\n" % self.target)
        self.assertEqual(wflib.read_target_path(self.app_dir), self.target)

    def test_all_comments_falls_through_to_prompt_target(self):
        self._write_target_file("# nothing valid here\n# still nothing\n")
        pdir = os.path.join(self.app_dir, "initial_prompt")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "initial_prompt.md"), "w", encoding="utf-8") as fh:
            fh.write("target: %s\n\nAudit it.\n" % self.target)
        self.assertEqual(wflib.read_target_path(self.app_dir), self.target)


class _FakeGr:
    def __init__(self, claim_result):
        self.claim_result = claim_result
        self.claims = 0
        self.releases = 0

    def claim_slot(self, *a, **k):
        self.claims += 1
        return self.claim_result

    def release(self, *a, **k):
        self.releases += 1


class TestGlobalCapClaimRelease(unittest.TestCase):
    """call_agent must release a machine-wide slot ONLY when it claimed one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_logdir = orch.LOG_DIR
        orch.LOG_DIR = self.tmp
        self._old_gr = orch.grlib
        self._old_runner = orch.RUNNERS.get("codex")
        orch.RUNNERS["codex"] = (
            lambda cfg, prompt, timeout: ("stub agent output", "", 0, "codex exec …"))

    def tearDown(self):
        orch.LOG_DIR = self._old_logdir
        orch.grlib = self._old_gr
        orch.RUNNERS["codex"] = self._old_runner

    def _cfg(self):
        return {"runtime": {"global_worker_cap_enabled": True,
                            "timeout_seconds_per_agent": 30}}

    def test_failed_claim_never_releases(self):
        fake = _FakeGr(claim_result=False)
        orch.grlib = fake
        out = orch.call_agent(self._cfg(), "app", "phase", 1, "codex", "hi")
        self.assertIn("stub agent output", out)
        self.assertEqual(fake.claims, 1)
        self.assertEqual(fake.releases, 0)   # proceeding uncapped: nothing to release

    def test_successful_claim_releases_exactly_once(self):
        fake = _FakeGr(claim_result=True)
        orch.grlib = fake
        orch.call_agent(self._cfg(), "app", "phase", 1, "codex", "hi")
        self.assertEqual(fake.claims, 1)
        self.assertEqual(fake.releases, 1)


class TestCodexInvocationUsesStdin(unittest.TestCase):
    """Codex must receive prompts over stdin; positional prompt args can trigger
    `Reading additional input from stdin...` in the CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_run_subprocess = orch._run_subprocess
        self._old_agent_cwd = orch._agent_cwd

    def tearDown(self):
        orch._run_subprocess = self._old_run_subprocess
        orch._agent_cwd = self._old_agent_cwd

    def test_run_codex_passes_prompt_on_stdin(self):
        captured = {}

        def fake_run_subprocess(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            captured["cmd"] = list(cmd)
            captured["input_text"] = input_text
            captured["cwd"] = cwd
            captured["timeout"] = timeout
            captured["heartbeat"] = heartbeat
            captured["env"] = env
            return "reply", "", 0

        orch._run_subprocess = fake_run_subprocess
        orch._agent_cwd = lambda _cfg: (self.tmp, False)
        cfg = {"_resolved": {"codex_model": "gpt-5.4"},
               "_allow_writes": False}
        out, err, code, displayed = orch.run_codex(cfg, "hello", timeout=17)

        self.assertEqual(out, "reply")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("<prompt on stdin>", displayed)
        self.assertEqual(captured["input_text"], "hello")
        self.assertIn("-", captured["cmd"])
        self.assertEqual(captured["cmd"][0:2], ["codex", "exec"])


class TestGeminiInvocationUsesStdin(unittest.TestCase):
    """Gemini must receive prompts over stdin, not argv: argv is visible to
    every local user via `ps`/`/proc/<pid>/cmdline`, and gemini-cli silently
    prefers -p over piped stdin when both are given, so -p must be omitted
    entirely rather than just adding stdin alongside it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_run_subprocess = orch._run_subprocess
        self._old_agent_cwd = orch._agent_cwd
        self._old_which = orch.which
        self._old_api_key = orch._gemini_api_key

    def tearDown(self):
        orch._run_subprocess = self._old_run_subprocess
        orch._agent_cwd = self._old_agent_cwd
        orch.which = self._old_which
        orch._gemini_api_key = self._old_api_key

    def _capture(self):
        captured = {}

        def fake_run_subprocess(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            captured["cmd"] = list(cmd)
            captured["input_text"] = input_text
            return "reply", "", 0

        orch._run_subprocess = fake_run_subprocess
        orch._agent_cwd = lambda _cfg: (self.tmp, False)
        return captured

    def test_api_key_path_passes_prompt_on_stdin(self):
        captured = self._capture()
        orch.which = lambda name: "/usr/bin/gemini" if name == "gemini" else None
        orch._gemini_api_key = lambda cfg: "test-key"
        cfg = {"_resolved": {"gemini_model": "gemini-2.5-pro"}, "_allow_writes": False}

        out, err, code, displayed = orch.run_gemini(cfg, "hello", timeout=17)

        self.assertEqual(out, "reply")
        self.assertIn("<prompt on stdin>", displayed)
        self.assertEqual(captured["input_text"], "hello")
        self.assertNotIn("-p", captured["cmd"])
        self.assertNotIn("hello", captured["cmd"])
        self.assertEqual(captured["cmd"][0], "gemini")
        # Must match the startup probe's invocation: without --skip-trust the
        # CLI can emit a trust prompt in the ephemeral tempdir and consume the
        # piped prompt as its answer.
        self.assertIn("--skip-trust", captured["cmd"])

    def test_keyless_fallback_passes_prompt_on_stdin(self):
        captured = self._capture()
        orch.which = lambda name: "/usr/bin/gemini" if name == "gemini" else None
        orch._gemini_api_key = lambda cfg: None
        cfg = {"_resolved": {"gemini_model": "gemini-2.5-pro"}, "_allow_writes": False,
               "runtime": {"gemini_use_agy": False}}

        out, err, code, displayed = orch.run_gemini(cfg, "hello", timeout=17)

        self.assertEqual(out, "reply")
        self.assertIn("<prompt on stdin>", displayed)
        self.assertEqual(captured["input_text"], "hello")
        self.assertNotIn("-p", captured["cmd"])
        self.assertNotIn("hello", captured["cmd"])
        self.assertEqual(captured["cmd"][0], "gemini")
        # The availability probe runs keyless with --skip-trust; the runtime
        # keyless path must use the same shape or the probe validates an
        # invocation that never actually runs.
        self.assertIn("--skip-trust", captured["cmd"])


class TestAgyInvocationUsesStdin(unittest.TestCase):
    """A-67: the agy (Antigravity) fallback must pipe the prompt over stdin
    like every other runner — on argv the full phase context is visible via
    `ps`, and a huge prompt E2BIGs execve. agy keeps the -p print-mode FLAG
    (verified: `agy -p` with no positional reads the whole prompt from stdin),
    but the prompt text itself must never appear in argv or the display cmd."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_run_subprocess = orch._run_subprocess
        self._old_agent_cwd = orch._agent_cwd
        self._old_which = orch.which
        self._old_api_key = orch._gemini_api_key

    def tearDown(self):
        orch._run_subprocess = self._old_run_subprocess
        orch._agent_cwd = self._old_agent_cwd
        orch.which = self._old_which
        orch._gemini_api_key = self._old_api_key

    def test_agy_path_passes_prompt_on_stdin(self):
        captured = {}

        def fake_run_subprocess(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            captured["cmd"] = list(cmd)
            captured["input_text"] = input_text
            return "reply", "", 0

        orch._run_subprocess = fake_run_subprocess
        orch._agent_cwd = lambda _cfg: (self.tmp, False)
        orch.which = lambda name: "/usr/bin/agy" if name == "agy" else None
        orch._gemini_api_key = lambda cfg: None
        cfg = {"_resolved": {"gemini_model": ""}, "_allow_writes": False}

        out, err, code, displayed = orch.run_gemini(cfg, "hello", timeout=17)

        self.assertEqual(out, "reply")
        self.assertEqual(captured["cmd"][0], "agy")
        self.assertEqual(captured["input_text"], "hello")
        self.assertNotIn("hello", captured["cmd"])
        self.assertIn("-p", captured["cmd"])   # print-mode flag stays
        self.assertIn("<prompt on stdin>", displayed)
        self.assertNotIn("hello", displayed)   # prompt never leaks into display


class TestPerWorkerHealthKey(unittest.TestCase):
    """Parallel-build workers pass _health_key so replicated lanes of one agent
    never share (and race on) a single circuit-breaker dict."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_logdir = orch.LOG_DIR
        orch.LOG_DIR = self.tmp
        self._old_runner = orch.RUNNERS.get("codex")
        orch.RUNNERS["codex"] = (
            lambda cfg, prompt, timeout: ("stub agent output", "", 0, "codex exec …"))

    def tearDown(self):
        orch.LOG_DIR = self._old_logdir
        orch.RUNNERS["codex"] = self._old_runner

    def test_health_keyed_by_worker_slug_when_set(self):
        cfg = {"_agent_health": {}, "_health_key": "codex-b",
               "runtime": {"timeout_seconds_per_agent": 30}}
        orch.call_agent(cfg, "app", "phase", "1.codex-b", "codex", "hi")
        self.assertIn("codex-b", cfg["_agent_health"])
        self.assertNotIn("codex", cfg["_agent_health"])

    def test_health_keyed_by_agent_on_sequential_path(self):
        cfg = {"_agent_health": {}, "runtime": {"timeout_seconds_per_agent": 30}}
        orch.call_agent(cfg, "app", "phase", 1, "codex", "hi")
        self.assertIn("codex", cfg["_agent_health"])

    def test_fallback_health_key_incorporates_lane_not_just_agent_and_step(self):
        # Two parallel-build lanes retrying the same agent+step on the fallback
        # ladder must land in DIFFERENT circuit-breaker health entries — a key
        # of just "fallback:<agent>:<step>" would let one lane's failures trip
        # the OTHER lane's breaker.
        orch.RUNNERS["codex"] = (
            lambda cfg, prompt, timeout: (_ for _ in ()).throw(orch.AgentError("boom")))
        self._old_fallback_steps = orch._fallback_steps
        orch._fallback_steps = lambda cfg, agent: ["gpt-5-mini"]
        self.addCleanup(setattr, orch, "_fallback_steps", self._old_fallback_steps)
        for lane in ("codex-a", "codex-b"):
            cfg = {"_agent_health": {}, "_health_key": lane,
                  "runtime": {"timeout_seconds_per_agent": 30}}
            with self.assertRaises(orch.AgentError):
                orch.call_agent(cfg, "app", "phase", 1, "codex", "hi")
            keys = [k for k in cfg["_agent_health"] if k.startswith("fallback:")]
            self.assertEqual(len(keys), 1, keys)
            self.assertIn(lane, keys[0])
        # Sanity: the two lanes produced genuinely distinct fallback keys.
        cfg_a = {"_agent_health": {}, "_health_key": "codex-a",
                "runtime": {"timeout_seconds_per_agent": 30}}
        cfg_b = {"_agent_health": {}, "_health_key": "codex-b",
                "runtime": {"timeout_seconds_per_agent": 30}}
        with self.assertRaises(orch.AgentError):
            orch.call_agent(cfg_a, "app", "phase", 1, "codex", "hi")
        with self.assertRaises(orch.AgentError):
            orch.call_agent(cfg_b, "app", "phase", 1, "codex", "hi")
        key_a = [k for k in cfg_a["_agent_health"] if k.startswith("fallback:")][0]
        key_b = [k for k in cfg_b["_agent_health"] if k.startswith("fallback:")][0]
        self.assertNotEqual(key_a, key_b)


class TestPruneLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _touch(self, name, age_days=0.0, size=10):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x" * size)
        if age_days:
            old = time.time() - age_days * 86400.0
            os.utime(p, (old, old))
        return p

    def test_old_call_logs_pruned_fresh_kept(self):
        old = self._touch("20250101_000000_000000__a__p__r1__codex.json", age_days=30)
        fresh = self._touch("20260703_000000_000000__a__p__r1__codex.json", age_days=1)
        keepme = self._touch("notes.txt", age_days=90)   # non-log files untouched
        orch.prune_logs(log_dir=self.tmp, retention_days=14)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(keepme))

    def test_retention_zero_disables_pruning(self):
        old = self._touch("20250101_000000_000000__a__p__r1__codex.json", age_days=400)
        orch.prune_logs(log_dir=self.tmp, retention_days=0)
        self.assertTrue(os.path.exists(old))

    def test_main_log_rotates_at_size_threshold(self):
        self._touch("orchestrator.log", size=2048)
        orch.prune_logs(log_dir=self.tmp, retention_days=14, max_log_bytes=1024)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "orchestrator.log")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "orchestrator.log.1")))

    def test_small_main_log_not_rotated(self):
        self._touch("orchestrator.log", size=64)
        orch.prune_logs(log_dir=self.tmp, retention_days=14, max_log_bytes=1024)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "orchestrator.log")))


class TestDerivedRunStatus(unittest.TestCase):
    def test_enum_precedence(self):
        self.assertEqual(orch.derive_run_status({}), "running")
        self.assertEqual(orch.derive_run_status({"done": True}), "done")
        self.assertEqual(orch.derive_run_status({"error": "boom"}), "aborted")
        self.assertEqual(orch.derive_run_status(
            {"awaiting_approval": "tech_specs"}), "awaiting_approval")
        self.assertEqual(orch.derive_run_status(
            {"enrollment_gate": {"phase": "enroll_report"}}),
            "enrolled_awaiting_approval")
        self.assertEqual(orch.derive_run_status(
            {"error": "x", "blocked_conflict": {"lane": "a"}}), "blocked_conflict")

    def test_save_state_writes_status_field(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        state = orch.load_state(tmp)
        orch.save_state(tmp, state)
        with open(orch.state_path(tmp), encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["status"], "running")
        state["done"] = True
        orch.save_state(tmp, state)
        with open(orch.state_path(tmp), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "done")


class TestAppSlugValidation(unittest.TestCase):
    def test_valid_names(self):
        for name in ("my-app", "tip-jar", "app2", "a_b"):
            self.assertTrue(orch.valid_app_slug(name), name)

    def test_traversal_and_paths_rejected(self):
        for name in ("", "..", "../x", "a/b", "a\\b", ".hidden", "/etc", "x/../y"):
            self.assertFalse(orch.valid_app_slug(name), repr(name))


class TestCallLogCommandRedaction(unittest.TestCase):
    def test_command_field_is_redacted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        old = orch.LOG_DIR
        orch.LOG_DIR = tmp
        try:
            secret = "sk-abcdefghij0123456789ABCDEFGHIJ"
            orch.write_call_log("app", "phase", 1, "codex",
                                "codex exec --api-key %s prompt" % secret,
                                "out", "err", 0)
        finally:
            orch.LOG_DIR = old
        files = [f for f in os.listdir(tmp) if f.endswith(".json")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(tmp, files[0]), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertNotIn(secret, record["command"])
        self.assertIn("[REDACTED", record["command"])

    def test_list_command_redacted_too(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        old = orch.LOG_DIR
        orch.LOG_DIR = tmp
        try:
            secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"
            orch.write_call_log("app", "phase", 1, "codex",
                                ["codex", "--token", secret], "out", "err", 0)
        finally:
            orch.LOG_DIR = old
        files = [f for f in os.listdir(tmp) if f.endswith(".json")]
        with open(os.path.join(tmp, files[0]), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertNotIn(secret, record["command"])


class TestRolesKnowledgeSmoke(unittest.TestCase):
    """Basic coverage for previously-untested modules (C092)."""

    def test_load_roles_returns_pools(self):
        personalities, roles = roleslib.load_roles(orch.HERE)
        self.assertTrue(personalities)
        self.assertTrue(roles)
        for r in roles:
            self.assertIn("id", r)
            self.assertIn("name", r)

    def test_load_roles_malformed_file_falls_back(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "roles.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json!!")
        personalities, roles = roleslib.load_roles(tmp)
        self.assertTrue(personalities)   # built-in defaults, never empty
        self.assertTrue(roles)

    def test_knowledge_domains_and_retrieve_never_raise(self):
        doms = knowlib.available_domains(orch.HERE)
        self.assertIsInstance(doms, list)
        out = knowlib.retrieve(orch.HERE, "ios", "swiftui state management",
                               max_chars=500, top_k=1)
        self.assertIsInstance(out, str)

    def test_seed_and_stream_scripts_import(self):
        import seed_demo       # noqa: F401
        import simulate_stream  # noqa: F401


if __name__ == "__main__":
    unittest.main()
