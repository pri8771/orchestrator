"""V3 board 1.11: message_produced attribution, mid-chat model swap, retry.

Attribution tests stub at the RUNNERS/resolve_runner layer (the
test_events.py pattern) so call_agent's REAL delivery paths run and emit;
the conversational swap/retry tests reuse the stubbed-loop fixture style
from test_conversational.py.
"""
import json
import os
import tempfile
import unittest

import events as evlib
import orchestrator as orch
import workflows as wf

KEY = "chat"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestMessageProducedAttribution(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_attr_")
        self.cfg = {"models": {"claude": "sonnet"},
                    "_resolved": {"claude_model": "sonnet"},
                    "_app_dir": self.app_dir}
        self._orig_resolve = orch.resolve_runner
        self._orig_installed = orch.lmlib.installed_models_cached
        self._orig_steps = orch._fallback_steps

    def tearDown(self):
        orch.resolve_runner = self._orig_resolve
        orch.lmlib.installed_models_cached = self._orig_installed
        orch._fallback_steps = self._orig_steps

    def test_direct_delivery_attributes_the_primary(self):
        orch.resolve_runner = lambda a: (
            lambda cfg, prompt, timeout: ("hello", "", 0, "claude -p"))
        out = orch.call_agent(self.cfg, "appx", KEY, 1, "claude", "hi")
        self.assertEqual(out, "hello")
        prod = evlib.read_events(self.app_dir, kinds=("message_produced",))
        self.assertEqual(len(prod), 1)
        self.assertEqual(prod[0]["agent"], "claude")
        self.assertEqual(prod[0]["model_used"], "sonnet")
        self.assertEqual(prod[0]["status"], "direct")

    def test_fallback_rescue_attributes_the_rescuer_not_the_roster_id(self):
        # Primary dies; a local model rescues. The event must carry the
        # RESCUER as model_used with status=fallback — the roster id stays,
        # which is exactly the join turn_completed could not give the GUI.
        def dispatch(agent):
            if str(agent).startswith("local:"):
                return lambda cfg, prompt, timeout: ("saved it", "", 0, "ollama")
            return lambda cfg, prompt, timeout: ("", "boom", 1, "claude -p")
        orch.resolve_runner = dispatch
        orch.lmlib.installed_models_cached = lambda: ["fakemodel"]
        orch._fallback_steps = lambda cfg, agent: ["local:fakemodel"]
        out = orch.call_agent(self.cfg, "appx", KEY, 2, "claude", "hi")
        self.assertIn("saved it", out)
        prod = evlib.read_events(self.app_dir, kinds=("message_produced",))
        self.assertEqual(len(prod), 1, "exactly one per DELIVERED reply")
        self.assertEqual(prod[0]["agent"], "claude")
        self.assertEqual(prod[0]["model_used"], "local:fakemodel")
        self.assertEqual(prod[0]["status"], "fallback")

    def test_failed_turn_emits_nothing(self):
        orch.resolve_runner = lambda a: (
            lambda cfg, prompt, timeout: ("", "down", 1, "claude -p"))
        orch._fallback_steps = lambda cfg, agent: []
        with self.assertRaises(orch.AgentError):
            orch.call_agent(self.cfg, "appx", KEY, 1, "claude", "hi")
        self.assertEqual(
            evlib.read_events(self.app_dir, kinds=("message_produced",)), [])


class ConversationalSwapBase(unittest.TestCase):
    """Conversational loop with stubbed sessioned turns (fixture style of
    test_conversational.py) capturing each turn's resolved model."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_swap_")
        os.makedirs(os.path.join(self.app_dir, "approvals"), exist_ok=True)
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_call = orch.call_agent
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent == "codex"
        self.turn_models = {}   # round -> codex model the turn saw
        self.on_round = {}

        def fake_sessioned(cfg, app, phase, rnd, agent, prompt,
                           delta_prompt=None, session_key=None):
            self.turn_models[rnd] = (cfg.get("_resolved") or {}).get("codex_model")
            hook = self.on_round.get(rnd)
            if hook:
                hook()
            return "reply %s" % rnd
        orch.call_agent_sessioned = fake_sessioned

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch.call_agent = self._orig_call
        orch._agent_available = self._orig_avail

    def _cfg(self):
        return {"agents": {"codex_enabled": True, "claude_enabled": False,
                           "gemini_enabled": False},
                "models": {"codex": "gpt-base"},
                "runtime": {"parallel_discussion_rounds": False,
                            "phase_quality_gates_enabled": False,
                            "phase_independent_first_round_enabled": False,
                            "approval_timeout_seconds": 30},
                "_workflow_target": "app", "_app_dir": self.app_dir,
                "root": self.app_dir}

    def _phase(self):
        return wf.Phase(KEY, ".", "chat.md", "talk", rounds=0,
                        conversational=True)

    def _state(self):
        return {"current_phase": None, "current_round": 0,
                "completed_phases": [], "phase_outputs": {},
                "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}

    def _inbox(self, msg):
        _write(os.path.join(self.app_dir, "human_inbox.txt"), msg)

    def _end(self):
        _write(os.path.join(self.app_dir, "approvals", "%s.ok" % KEY), "")

    def _md(self):
        with open(os.path.join(self.app_dir, "chat.md"), encoding="utf-8") as fh:
            return fh.read()


class TestMidChatSwapAndRetry(ConversationalSwapBase):
    def test_routing_edit_applies_next_round_history_untouched(self):
        self._inbox("first")

        def swap_then_message():
            _write(os.path.join(self.app_dir, "model_routing.json"),
                   json.dumps({"schema_version": 1, "enabled": True,
                               "phases": {KEY: {"codex": "gpt-swapped"}}}))
            self._inbox("second")
        self.on_round[1] = swap_then_message
        self.on_round[2] = self._end
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], self._state())
        self.assertEqual(self.turn_models[2], "gpt-swapped",
                         "the swap must apply at the next round barrier")
        self.assertNotEqual(self.turn_models[1], "gpt-swapped",
                            "round 1 ran on the pre-swap model")
        text = self._md()
        # Zero history rewrite: round 1's block is intact, exactly once.
        self.assertEqual(text.count("reply 1"), 1)
        self.assertEqual(text.count("### Round 1"), 1)

    def test_retry_reruns_one_agent_stateless_with_patched_model(self):
        captured = {}

        def fake_call_agent(cfg, app, phase, rnd, agent, prompt):
            captured["agent"] = agent
            captured["codex_model"] = (cfg.get("_resolved") or {}).get("codex_model")
            captured["session"] = cfg.get("_session")
            # End the chat once the retry has been delivered.
            self._end()
            return "retried take"
        orch.call_agent = fake_call_agent

        self._inbox("first")

        def request_retry():
            _write(os.path.join(self.app_dir, "approvals", "%s.retry" % KEY),
                   json.dumps({"agent": "codex", "model": "gpt-retry"}))
        self.on_round[1] = request_retry
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], self._state())
        text = self._md()
        self.assertEqual(text.count("(retry on gpt-retry) — Round 1"), 1,
                         "exactly one explicitly labeled retry block")
        self.assertEqual(text.count("reply 1"), 1, "original reply preserved")
        self.assertEqual(captured["agent"], "codex")
        self.assertEqual(captured["codex_model"], "gpt-retry")
        self.assertIsNone(captured["session"], "retry must be stateless")
        # Rename-then-run: the request file is consumed exactly once.
        appr = os.path.join(self.app_dir, "approvals")
        self.assertFalse(os.path.exists(os.path.join(appr, "%s.retry" % KEY)))
        self.assertTrue(os.path.exists(os.path.join(appr, "%s.retry.consumed" % KEY)))

    def test_garbage_retry_request_is_ignored_not_fatal(self):
        self._inbox("first")

        def bad_retry_then_end():
            _write(os.path.join(self.app_dir, "approvals", "%s.retry" % KEY),
                   "not json at all")
            self._end()
        self.on_round[1] = bad_retry_then_end
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], self._state())
        self.assertNotIn("(retry on", self._md())
        self.assertIn(KEY, orch.load_state(self.app_dir)["completed_phases"])


if __name__ == "__main__":
    unittest.main()
