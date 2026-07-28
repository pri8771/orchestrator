"""Tests for CLI-session reuse (NEXT_MILESTONES.md item 3): only claude got
delta prompts before; codex now does too, but scoped to write-enabled (build)
phases only, since `codex exec resume` has no --sandbox flag and always runs
workspace-write regardless of what the original session used — verified
against a real codex install, not assumed. Gemini is deliberately not
included yet (see call_agent_sessioned's docstring)."""
import shutil
import tempfile
import unittest

import orchestrator as orch


class TestRunCodexSessionArgv(unittest.TestCase):
    """run_codex's command shape for a fresh session vs. a resumed one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_run_subprocess = orch._run_subprocess
        self._old_agent_cwd = orch._agent_cwd

    def tearDown(self):
        orch._run_subprocess = self._old_run_subprocess
        orch._agent_cwd = self._old_agent_cwd

    def _capture(self, out=""):
        captured = {}

        def fake_run_subprocess(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
            captured["cmd"] = list(cmd)
            captured["input_text"] = input_text
            return out, "", 0

        orch._run_subprocess = fake_run_subprocess
        orch._agent_cwd = lambda _cfg: (self.tmp, False)
        return captured

    def test_fresh_session_uses_plain_exec_with_sandbox(self):
        captured = self._capture()
        cfg = {"_resolved": {"codex_model": ""}, "_allow_writes": True}
        orch.run_codex(cfg, "hello", timeout=17)
        self.assertEqual(captured["cmd"][0:2], ["codex", "exec"])
        self.assertIn("--sandbox", captured["cmd"])
        self.assertNotIn("resume", captured["cmd"])

    def test_resume_uses_resume_subcommand_with_id_no_sandbox(self):
        captured = self._capture()
        cfg = {"_resolved": {"codex_model": ""}, "_allow_writes": True,
               "_session": {"id": "019f6730-1095-7fc3-9478-5b154643ef0d", "resume": True}}
        orch.run_codex(cfg, "hello", timeout=17)
        self.assertEqual(captured["cmd"][0:3],
                         ["codex", "exec", "resume"])
        self.assertIn("019f6730-1095-7fc3-9478-5b154643ef0d", captured["cmd"])
        self.assertNotIn("--sandbox", captured["cmd"])

    def test_fresh_session_extracts_id_from_banner(self):
        banner = (
            "OpenAI Codex v0.144.2\n--------\nworkdir: /tmp\nmodel: gpt-5.6-sol\n"
            "provider: openai\napproval: never\nsandbox: workspace-write\n"
            "reasoning effort: low\nreasoning summaries: none\n"
            "session id: 019f6730-1095-7fc3-9478-5b154643ef0d\n--------\n"
            "user\nhello\n\ncodex\nOK\n"
        )
        self._capture(out=banner)
        # Matches call_agent_sessioned's real convention: it marks "I'm
        # tracking a session for this call" with {"id": None, "resume":
        # False} before the session-creating call, so run_codex knows to
        # bother extracting an id at all.
        cfg = {"_resolved": {"codex_model": ""}, "_allow_writes": True,
               "_session": {"id": None, "resume": False}}
        orch.run_codex(cfg, "hello", timeout=17)
        self.assertEqual(cfg.get("_new_session_id"),
                         "019f6730-1095-7fc3-9478-5b154643ef0d")

    def test_resumed_call_never_sets_new_session_id(self):
        banner = "session id: 019f6730-1095-7fc3-9478-5b154643ef0d\ncodex\nOK\n"
        self._capture(out=banner)
        cfg = {"_resolved": {"codex_model": ""}, "_allow_writes": True,
               "_session": {"id": "019f6730-1095-7fc3-9478-5b154643ef0d", "resume": True}}
        orch.run_codex(cfg, "hello", timeout=17)
        self.assertNotIn("_new_session_id", cfg)

    def test_no_banner_match_leaves_new_session_id_unset(self):
        self._capture(out="no session banner here\ncodex\nOK\n")
        cfg = {"_resolved": {"codex_model": ""}, "_allow_writes": True,
               "_session": {"id": None, "resume": False}}
        orch.run_codex(cfg, "hello", timeout=17)
        self.assertNotIn("_new_session_id", cfg)


class TestCallAgentSessioned(unittest.TestCase):
    def setUp(self):
        self._orig_call_agent = orch.call_agent

    def tearDown(self):
        orch.call_agent = self._orig_call_agent

    def _fake(self, banner_sid=None):
        calls = []

        def fake(cfg, app, phase, rnd, agent, prompt):
            calls.append({"prompt": prompt, "session": cfg.get("_session")})
            if agent == "codex" and cfg.get("_session") and not cfg["_session"].get("resume") \
                    and banner_sid:
                cfg["_new_session_id"] = banner_sid
            return "reply"

        orch.call_agent = fake
        return calls

    def test_codex_reuses_session_only_when_allow_writes(self):
        calls = self._fake(banner_sid="SID-1")
        cfg = {"_allow_writes": True}
        orch.call_agent_sessioned(cfg, "app", "build_coordination", 1, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        orch.call_agent_sessioned(cfg, "app", "build_coordination", 2, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["prompt"], "FULL")
        self.assertFalse(calls[0]["session"]["resume"])
        self.assertEqual(calls[1]["prompt"], "DELTA")
        self.assertTrue(calls[1]["session"]["resume"])
        self.assertEqual(calls[1]["session"]["id"], "SID-1")

    def test_codex_never_sessions_in_discussion_phase(self):
        calls = self._fake(banner_sid="SID-2")
        cfg = {"_allow_writes": False}
        orch.call_agent_sessioned(cfg, "app", "initial_discussion", 1, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        orch.call_agent_sessioned(cfg, "app", "initial_discussion", 2, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        # Both calls get the FULL prompt — no resume ever happens read-only.
        self.assertEqual([c["prompt"] for c in calls], ["FULL", "FULL"])
        self.assertIsNone(calls[0]["session"])
        self.assertIsNone(calls[1]["session"])

    def test_codex_session_reuse_config_flag_disables_it(self):
        calls = self._fake(banner_sid="SID-3")
        cfg = {"_allow_writes": True, "runtime": {"codex_session_reuse": False}}
        orch.call_agent_sessioned(cfg, "app", "build_coordination", 1, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        orch.call_agent_sessioned(cfg, "app", "build_coordination", 2, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        self.assertEqual([c["prompt"] for c in calls], ["FULL", "FULL"])

    def test_claude_still_sessions_regardless_of_allow_writes(self):
        calls = self._fake()
        cfg = {"_allow_writes": False}
        orch.call_agent_sessioned(cfg, "app", "initial_discussion", 1, "claude",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        orch.call_agent_sessioned(cfg, "app", "initial_discussion", 2, "claude",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        self.assertEqual(calls[1]["prompt"], "DELTA")
        self.assertTrue(calls[1]["session"]["resume"])
        # claude self-assigns a uuid up front — unlike codex it never depends
        # on scraping one back out of the response.
        self.assertIsNotNone(calls[0]["session"]["id"])

    def test_resumed_codex_call_failure_falls_back_to_stateless_full_prompt(self):
        state = {"n": 0}

        def flaky(cfg, app, phase, rnd, agent, prompt):
            state["n"] += 1
            if state["n"] == 1:
                cfg["_new_session_id"] = "SID-4"
                return "reply"
            if cfg.get("_session") and cfg["_session"].get("resume"):
                raise orch.AgentError("session expired")
            return "recovered:" + prompt

        orch.call_agent = flaky
        cfg = {"_allow_writes": True}
        orch.call_agent_sessioned(cfg, "app", "build_coordination", 1, "codex",
                                  "FULL", delta_prompt="DELTA", session_key="k")
        out = orch.call_agent_sessioned(cfg, "app", "build_coordination", 2, "codex",
                                        "FULL", delta_prompt="DELTA", session_key="k")
        self.assertEqual(out, "recovered:FULL")
        self.assertNotIn("k", cfg["_codex_sessions"])

    def test_no_session_key_or_delta_is_always_stateless(self):
        calls = self._fake()
        cfg = {"_allow_writes": True}
        orch.call_agent_sessioned(cfg, "app", "build_coordination", 1, "codex", "FULL")
        self.assertEqual(calls[0]["prompt"], "FULL")
        self.assertIsNone(calls[0]["session"])


class TestFallbackRespectsSessions(unittest.TestCase):
    """The fallback ladder must not hijack sessioned calls: a RESUMED call's
    delta prompt is meaningless to a stateless rescue model (it holds none of
    the phase context), and a ladder-rescued FIRST call ran stateless, so its
    pre-picked session id backs no real session and must not be recorded."""

    def setUp(self):
        self._once = orch._call_agent_once
        self._steps = orch._fallback_steps
        self._installed = orch.lmlib.installed_models_cached
        orch.lmlib.installed_models_cached = lambda: {}

    def tearDown(self):
        orch._call_agent_once = self._once
        orch._fallback_steps = self._steps
        orch.lmlib.installed_models_cached = self._installed

    def test_resumed_call_failure_reraises_instead_of_delta_fallback(self):
        prompts = []

        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            prompts.append(prompt)
            raise orch.AgentError("usage cap")

        orch._call_agent_once = once
        orch._fallback_steps = lambda cfg, agent: ["backup-model"]
        cfg = {"_session": {"id": "S1", "resume": True}}
        with self.assertRaises(orch.AgentError):
            orch.call_agent(cfg, "app", "tech_specs", 2, "claude", "DELTA")
        # The ladder must NOT have retried the delta prompt statelessly —
        # call_agent_sessioned's except-arm owns this failure (full prompt).
        self.assertEqual(prompts, ["DELTA"])

    def test_rescued_first_call_does_not_store_session_id(self):
        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            if (cfg.get("_session") or {}).get("id"):
                raise orch.AgentError("primary down")   # the sessioned primary
            return "rescued"                            # the stateless ladder step

        orch._call_agent_once = once
        orch._fallback_steps = lambda cfg, agent: ["backup-model"]
        cfg = {"_allow_writes": False, "_resolved": {}}
        out = orch.call_agent_sessioned(
            cfg, "app", "initial_discussion", 1, "claude",
            "FULL", delta_prompt="DELTA", session_key="k")
        self.assertIn("rescued", out)
        self.assertTrue(out.startswith("_[Fallback: "))
        self.assertNotIn("k", cfg.get("_claude_sessions", {}))


if __name__ == "__main__":
    unittest.main()
