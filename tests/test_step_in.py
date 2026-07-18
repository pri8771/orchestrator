"""V3 board 1.7: "Step in" — a marker pauses a live auto debate at the next
round barrier so the human's message is drained and responded to.

Same stubbed-agent fixture family as test_round_resume.py. The fake agent's
per-round hooks write the marker/message mid-round, exactly like a GUI user
typing while agents talk.
"""
import json
import os
import tempfile
import threading
import time
import unittest

import orchestrator as orch
import workflows as wf

KEY = "design_discussion"


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class StepInBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent == "codex"
        self.calls = []          # (round, agent, session_key)
        self.prompts = {}        # (round, kind) -> prompt text
        self.on_round = {}       # round -> callable, runs DURING the agent turn
        self.consensus_round = 3

        self.on_coord = {}       # round -> callable, runs DURING the coord turn

        def fake_sessioned(cfg, app, phase, rnd, agent, prompt,
                           delta_prompt=None, session_key=None):
            self.calls.append((rnd, agent, session_key))
            kind = "coord" if (session_key or "").endswith(":coord") else "agent"
            self.prompts[(rnd, kind)] = prompt
            if kind == "agent":
                hook = self.on_round.get(rnd)
                if hook:
                    hook()
                return "take for round %s" % rnd
            chook = self.on_coord.get(rnd)
            if chook:
                chook()
            if rnd >= self.consensus_round:
                return "Aligned. CONSENSUS: YES\n\n## Final Output\n\nDone.\n"
            return "Keep going. CONSENSUS: NO"
        orch.call_agent_sessioned = fake_sessioned

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch._agent_available = self._orig_avail

    def _cfg(self, step_wait=5, independent_first=False):
        return {"agents": {"codex_enabled": True, "claude_enabled": False,
                           "gemini_enabled": False},
                "runtime": {"parallel_discussion_rounds": False,
                            "phase_quality_gates_enabled": False,
                            "phase_independent_first_round_enabled": independent_first,
                            "step_in_wait_seconds": step_wait},
                "_workflow_target": "app", "_app_dir": self.app_dir,
                "root": self.app_dir}

    def _phase(self, rounds=4):
        return wf.Phase(KEY, ".", "d.md", "purpose", rounds=rounds)

    def _state(self):
        return {"current_phase": None, "current_round": 0,
                "completed_phases": [], "phase_outputs": {},
                "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}

    def _marker(self):
        _write(os.path.join(self.app_dir, ".step_in"), "")

    def _inbox(self, msg):
        _write(os.path.join(self.app_dir, "human_inbox.txt"), msg)

    def _md(self):
        with open(os.path.join(self.app_dir, "d.md"), encoding="utf-8") as fh:
            return fh.read()

    def _events(self, kind=None):
        p = os.path.join(self.app_dir, "events.jsonl")
        if not os.path.exists(p):
            return []
        evs = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        return [e for e in evs if kind is None or e.get("kind") == kind]


class TestStepIn(StepInBase):
    def test_mid_round_step_in_is_folded_early_and_agents_respond_next_round(self):
        # User types while round 1's agents talk: the pre-coordinator drain
        # folds the message the same round (that IS the join — the marker is
        # consumed there), and round 2's agents genuinely respond to it.
        def user_types():
            self._marker()
            self._inbox("please consider offline mode")
        self.on_round[1] = user_types
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "prompt", [], self._state())
        text = self._md()
        human = text.index("**You (human) — Round 1**")
        agent_r2 = text.index("take for round 2")   # header carries a persona hat
        self.assertLess(human, agent_r2)
        # Round 2's agent context contained the human message.
        self.assertIn("offline mode", self.prompts[(2, "agent")])
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, ".step_in")))
        joined = self._events("step_in_joined")
        self.assertEqual([e["round"] for e in joined], [1])

    def test_consensus_is_deferred_one_round_after_a_pre_coordinator_join(self):
        # Without the deferral, the coordinator could declare consensus in the
        # same breath as reading the human's late message — closing the phase
        # before any agent responded to it.
        self.consensus_round = 1

        def user_types():
            self._marker()
            self._inbox("hold on — what about privacy?")
        self.on_round[1] = user_types
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "prompt", [], self._state())
        text = self._md()
        self.assertIn("Consensus deferred", text)
        self.assertIn("privacy", self.prompts[(2, "agent")],
                      "round 2's agents must see and respond to the human")
        self.assertIn("### Round 2", text)
        # The phase then closed normally (consensus at round 2's coordinator).
        self.assertIn("CONSENSUS: YES", text)

    def test_marker_before_message_waits_for_the_racing_write(self):
        # Marker lands first (the GUI's ordering); the message arrives a beat
        # later — the barrier's short wait picks it up instead of missing it.
        self.on_round[1] = self._marker
        writer = threading.Timer(0.5, lambda: self._inbox("late but real"))
        writer.start()
        try:
            orch.process_phase(self._cfg(step_wait=5), "demo", self.app_dir,
                               self._phase(), "prompt", [], self._state())
        finally:
            writer.cancel()
        self.assertIn("late but real", self._md())
        self.assertEqual([e["round"] for e in self._events("step_in_joined")], [2])

    def test_stale_marker_empty_inbox_never_deadlocks(self):
        self.on_round[1] = self._marker   # marker, but no message ever
        t0 = time.monotonic()
        orch.process_phase(self._cfg(step_wait=0.4), "demo", self.app_dir,
                           self._phase(), "prompt", [], self._state())
        self.assertLess(time.monotonic() - t0, 10, "bounded wait, no stall")
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, ".step_in")))
        missed = self._events("step_in_missed")
        self.assertTrue(missed)
        self.assertNotIn("You (human)", self._md())

    def test_independent_first_hiding_yields_to_a_joined_human(self):
        # Round-1 blank-room hiding must not hide the human the agents are
        # supposed to react to.
        self._marker()
        self._inbox("react to me specifically")
        orch.process_phase(self._cfg(independent_first=True), "demo",
                           self.app_dir, self._phase(), "prompt", [], self._state())
        self.assertIn("react to me specifically", self.prompts[(1, "agent")],
                      "round-1 context must include the joined human block")
        self.assertEqual([e["round"] for e in self._events("step_in_joined")], [1])

    def test_phase_end_before_join_is_surfaced_and_message_preserved(self):
        # The step-in lands DURING the coordinator's consensus turn — after
        # the last drain of the phase. It can never be honored: the marker is
        # cleared, the miss is surfaced, and the message stays in the inbox.
        self.consensus_round = 1

        def too_late():
            self._marker()
            self._inbox("wait, one more thing")
        self.on_coord[1] = too_late
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "prompt", [], self._state())
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, ".step_in")))
        missed = self._events("step_in_missed")
        self.assertEqual(len(missed), 1)
        self.assertIn("preserved", missed[0]["detail"])
        with open(os.path.join(self.app_dir, "human_inbox.txt"),
                  encoding="utf-8") as fh:
            self.assertIn("one more thing", fh.read())

    def test_no_marker_is_byte_identical_behavior(self):
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "prompt", [], self._state())
        self.assertEqual(self._events("step_in_requested"), [])
        self.assertEqual(self._events("step_in_missed"), [])
        self.assertNotIn("step_in", self._md())


if __name__ == "__main__":
    unittest.main()
