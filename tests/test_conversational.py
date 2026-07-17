"""V3 board 1.1: the `conversational` phase flag.

Drives process_phase() end-to-end with stubbed agents, the same pattern as
test_round_resume.py. Determinism note: instead of timing-fragile writer
threads, the FAKE AGENT writes the next human message / end command during
its own turn — the subsequent _await_inbox check then wakes immediately, so
no test depends on the 2s poll cadence.

Covers the card's gate: scripted-inbox happy path, end-command path (end
wins + closing-message drain), idle-timeout path, crash-resume append-only
path, no-coordinator-block assertion, schema round-trip, stale-end-command
semantics (cleared on fresh start, honored on resume), and awaiting-human
marker hygiene.
"""
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf

KEY = "chat"


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class ConversationalBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "approvals"), exist_ok=True)
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent == "codex"
        self.calls = []
        # {round: callable} — side effects the fake agent runs DURING its turn
        # (write the next inbox message, drop the end command, …).
        self.on_round = {}

        def fake_sessioned(cfg, app, phase, rnd, agent, prompt,
                           delta_prompt=None, session_key=None):
            self.calls.append((rnd, agent, session_key))
            hook = self.on_round.get(rnd)
            if hook:
                hook()
            return "chat reply for round %s" % rnd
        orch.call_agent_sessioned = fake_sessioned

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch._agent_available = self._orig_avail

    def _cfg(self, idle_timeout=30):
        return {"agents": {"codex_enabled": True, "claude_enabled": False,
                           "gemini_enabled": False},
                "runtime": {"parallel_discussion_rounds": False,
                            "phase_quality_gates_enabled": False,
                            "phase_independent_first_round_enabled": False,
                            "approval_timeout_seconds": idle_timeout},
                "_workflow_target": "app", "_app_dir": self.app_dir,
                "root": self.app_dir}

    def _phase(self):
        # rounds=1 on purpose: the conversational loop must IGNORE it (the GUI
        # clamps rounds to 1..9 on save, so honoring it would cap the chat).
        return wf.Phase(KEY, ".", "chat.md", "talk with the human",
                        rounds=1, conversational=True)

    def _state(self, **over):
        st = {"current_phase": None, "current_round": 0,
              "completed_phases": [], "phase_outputs": {},
              "consensus_status": {}, "vote_results": {}, "prompt_hash": "h"}
        st.update(over)
        return st

    def _inbox(self, msg):
        _write(os.path.join(self.app_dir, "human_inbox.txt"), msg)

    def _end(self):
        _write(os.path.join(self.app_dir, "approvals", "%s.ok" % KEY), "")

    def _md(self):
        with open(os.path.join(self.app_dir, "chat.md"), encoding="utf-8") as fh:
            return fh.read()


class TestConversationalPhase(ConversationalBase):
    def test_single_round_end_command(self):
        self._inbox("hello agents")
        self.on_round[1] = self._end
        state = self._state()
        out = orch.process_phase(self._cfg(), "demo", self.app_dir,
                                 self._phase(), "seed prompt", [], state)
        text = self._md()
        # Human message interleaved, agent replied, round structure present.
        self.assertIn("### Round 1", text)
        self.assertIn("**You (human) — Round 1**", text)
        self.assertIn("hello agents", text)
        self.assertIn("chat reply for round 1", text)
        # No coordinator debate block, no fabricated consensus/vote markers —
        # but the literal heading survives (the GUI parser needs it).
        self.assertNotIn("**Coordinator (", text)
        self.assertIn("## Coordinator Decision", text)
        self.assertIn("No coordinator", text)
        self.assertNotIn("CONSENSUS: YES", text)
        self.assertNotIn("VOTE_DECISION", text)
        self.assertIn("ENDED BY USER", text)
        # Honest state: clean user-ended close, not an unresolved one.
        self.assertIn(KEY, state["completed_phases"])
        self.assertFalse(state["consensus_status"][KEY])
        self.assertEqual(state["conversation_end"][KEY], "ended by user")
        self.assertNotIn(KEY, state.get("phase_resolutions", {}))
        self.assertIsNone(state.get("awaiting_human"))
        # final_output is a closure note, not the transcript.
        self.assertNotIn("hello agents", out)
        self.assertIn("ended by user", out)
        # rounds=1 was ignored as such — the end command decided, not the cap.
        self.assertEqual([r for r, _a, _sk in self.calls], [1])

    def test_multi_round_message_then_end(self):
        self._inbox("first question")
        self.on_round[1] = lambda: self._inbox("follow-up question")
        self.on_round[2] = self._end
        state = self._state()
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], state)
        text = self._md()
        # Both human messages interleaved in send order, two rounds ran —
        # i.e. the Phase rounds=1 budget was ignored (unbounded chat).
        self.assertLess(text.index("first question"),
                        text.index("follow-up question"))
        self.assertIn("### Round 2", text)
        self.assertEqual([r for r, _a, _sk in self.calls], [1, 2])
        self.assertEqual(state["conversation_end"][KEY], "ended by user")

    def test_end_wins_and_final_message_is_drained(self):
        # A message typed just before End must not rot in the inbox: it is
        # folded in as a closing section, then the chat finalizes.
        self._inbox("opening")

        def end_with_pending_message():
            self._inbox("one last thought")
            self._end()
        self.on_round[1] = end_with_pending_message
        state = self._state()
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], state)
        text = self._md()
        self.assertIn("one last thought", text)
        self.assertIn("closing message", text)   # drained under the closing label
        self.assertEqual(state["conversation_end"][KEY], "ended by user")
        # Inbox is empty afterwards — nothing rots.
        with open(os.path.join(self.app_dir, "human_inbox.txt"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "")

    def test_idle_timeout_finalizes_honestly(self):
        # timeout=1, not 0: _approval_timeout coerces falsy to the 7200s
        # default (`0 or 7200`), so 0 would wait two hours, not zero seconds.
        self._inbox("hello")
        state = self._state()
        orch.process_phase(self._cfg(idle_timeout=1), "demo", self.app_dir,
                           self._phase(), "seed", [], state)
        text = self._md()
        self.assertIn("CONVERSATION CLOSED: CONVERSATION IDLE TIMEOUT", text)
        self.assertNotIn("ENDED BY USER", text)
        self.assertIn(KEY, state["completed_phases"])
        self.assertEqual(state["conversation_end"][KEY],
                         "conversation idle timeout")
        # An idle timeout IS an unresolved close — unlike ended-by-user.
        self.assertEqual(state["phase_resolutions"][KEY], "idle_timeout")
        self.assertIsNone(state.get("awaiting_human"))

    def test_crash_resume_is_append_only_and_honors_pending_end(self):
        # Pre-seed a crashed conversation: round 1 recorded, NO coordinator
        # block (a conversational transcript never has one) — the stock
        # _resume_round_state heuristic would discard it.
        existing = ("# Demo — Chat\n\n## Transcript\n\n"
                    "\n### Round 1\n\n"
                    "**You (human) — Round 1**\n\nfirst take from before crash\n"
                    "\n**Codex — Round 1**\n\nold reply\n")
        _write(os.path.join(self.app_dir, "chat.md"), existing)
        # The user ended the chat while the engine was down: a pre-existing
        # end command on the RESUME path is a real command, not stale.
        self._end()
        state = self._state(current_phase=KEY, current_round=1)
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], state)
        text = self._md()
        # Append-only: zero previously-recorded rounds discarded.
        self.assertIn("first take from before crash", text)
        self.assertIn("old reply", text)
        self.assertEqual(text.count("### Round 1"), 1)
        # Resumed at round 2, ran it once, then honored the pending end.
        self.assertIn("### Round 2", text)
        self.assertEqual([r for r, _a, _sk in self.calls], [2])
        self.assertEqual(state["conversation_end"][KEY], "ended by user")

    def test_fresh_start_clears_stale_end_command(self):
        # A leftover .ok from an unrelated earlier attempt must NOT insta-end
        # round 1 of a brand-new chat.
        self._end()
        self._inbox("hello")
        self.on_round[1] = lambda: self._inbox("more")
        self.on_round[2] = self._end
        state = self._state()   # fresh: current_phase None
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], state)
        # Two rounds ran — the stale end command did not terminate round 1.
        self.assertEqual([r for r, _a, _sk in self.calls], [1, 2])
        self.assertEqual(state["conversation_end"][KEY], "ended by user")


class TestPhaseSchemaRoundTrip(unittest.TestCase):
    def test_conversational_round_trips(self):
        p = wf.Phase("c", "c", "c.md", "p", conversational=True)
        d = p.to_json()
        self.assertTrue(d["conversational"])
        self.assertTrue(wf.Phase.from_json(d).conversational)

    def test_legacy_json_defaults_false(self):
        p = wf.Phase.from_json({"key": "x"})
        self.assertFalse(p.conversational)
        self.assertFalse(wf.Phase("x", "x", "x.md", "p").conversational)


if __name__ == "__main__":
    unittest.main()


class TestAwaitInboxCadence(unittest.TestCase):
    """V3 board 1.2: 250ms stat-gated tick — wake latency, idle cost, robustness."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.app_dir, "approvals"), exist_ok=True)
        self.state = {"completed_phases": [], "phase_outputs": {},
                      "consensus_status": {}, "vote_results": {}}

    def _wait(self, timeout):
        return orch._await_inbox({}, "demo", self.app_dir, KEY, self.state,
                                 timeout=timeout)

    def test_message_wakes_in_under_750ms(self):
        import threading
        import time as _t

        def writer():
            _t.sleep(0.3)
            _write(os.path.join(self.app_dir, "human_inbox.txt"), "hi there")
        th = threading.Thread(target=writer)
        th.start()
        t0 = _t.monotonic()
        decision, _ = self._wait(timeout=5)
        elapsed = _t.monotonic() - t0
        th.join()
        self.assertEqual(decision, "message")
        self.assertLess(elapsed, 0.75,
                        "wake took %.2fs — the 250ms tick is not in effect" % elapsed)

    def test_idle_wait_does_zero_content_reads_and_bounded_cpu(self):
        import time as _t
        # A pre-existing whitespace-only inbox: read once on the first tick
        # (stat differs from the sentinel), then never again while unchanged.
        _write(os.path.join(self.app_dir, "human_inbox.txt"), "   \n")
        reads = []
        orig = orch._read_nonblank

        def counting(path):
            reads.append(path)
            return orig(path)
        orch._read_nonblank = counting
        try:
            cpu0 = _t.process_time()
            decision, _ = self._wait(timeout=1.0)
            cpu = _t.process_time() - cpu0
        finally:
            orch._read_nonblank = orig
        self.assertEqual(decision, "timeout")
        self.assertLessEqual(len(reads), 1,
                             "idle ticks re-read the inbox %d times" % len(reads))
        self.assertLess(cpu, 0.05, "idle wait burned %.0fms CPU" % (cpu * 1000))

    def test_missing_inbox_never_crashes_and_end_still_wakes(self):
        import threading
        import time as _t

        def ender():
            _t.sleep(0.3)
            _write(os.path.join(self.app_dir, "approvals", "%s.ok" % KEY), "")
        th = threading.Thread(target=ender)
        th.start()
        t0 = _t.monotonic()
        decision, _ = self._wait(timeout=5)
        elapsed = _t.monotonic() - t0
        th.join()
        self.assertEqual(decision, "end")
        self.assertLess(elapsed, 0.75)
        self.assertIsNone(self.state.get("awaiting_human"))
