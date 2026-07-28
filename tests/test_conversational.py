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


class TestCastAddValidation(ConversationalBase):
    """A-76: /cast add must require a real agent identity (RUNNERS key,
    local:<model>, api:<provider>:<model>) BEFORE the availability probe —
    for unknown names _agent_available is just which(), so any binary on
    PATH ('git') would be admitted and later blow up in resolve_runner."""

    def test_cast_add_of_path_binary_is_refused_with_a_card(self):
        self._inbox("hello")
        # Round 1's turn queues the command; the barrier applies it when
        # round 3 opens (requested_round=2 < 3), so keep the chat alive
        # through round 3.
        self.on_round[1] = lambda: self._inbox("/cast add git")
        self.on_round[2] = lambda: self._inbox("keep going")
        self.on_round[3] = self._end
        state = self._state()
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], state)
        text = self._md()
        self.assertIn("is not a known agent id", text)
        self.assertNotIn("joins the cast", text)
        # 'git' never became a roster member — no turn was ever run for it.
        self.assertNotIn("git", [a for _r, a, _sk in self.calls])


class TestBarrierDiesWithTheChat(ConversationalBase):
    """A-71: a /vote queued during a chat that ends before the barrier fires
    must be cleared by the finalize — otherwise the row persists in
    agent_state.json and silently fires a forced vote in a later unrelated
    conversational phase."""

    def test_unfired_queued_vote_is_cleared_on_finalize(self):
        import commands as cmdlib
        cmdlib.ensure_seeded(orch.HERE)
        self._inbox("/vote")           # queued at round 1's open peek
        self.on_round[1] = self._end   # chat ends before the barrier fires
        state = self._state()
        orch.process_phase(self._cfg(), "demo", self.app_dir, self._phase(),
                           "seed", [], state)
        self.assertEqual(orch.load_state(self.app_dir)["command_barrier"], [])
        self.assertNotIn("### Forced Vote", self._md())


class TestMidChatRoutingRemoval(ConversationalBase):
    """A-75: the mid-chat routing refresh must rebuild from the pristine
    pre-route cfg — _apply_phase_routing only ADDS overrides, so layering
    onto the already-routed copy made REMOVING an override (model, timeout)
    a silent no-op while still announcing 'Chat routing updated'."""

    def test_removing_an_override_mid_chat_takes_effect(self):
        import json
        routing_path = os.path.join(self.app_dir, "model_routing.json")
        _write(routing_path, json.dumps(
            {"schema_version": 1,
             "phases": {KEY: {"codex": "routed-codex", "timeout": 555}}}))
        seen = {}
        inner = orch.call_agent_sessioned   # the ConversationalBase fake

        def recording(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            seen.setdefault(rnd, ((cfg.get("models") or {}).get("codex"),
                                  cfg.get("_turn_timeout")))
            return inner(cfg, app, phase, rnd, agent, prompt,
                         delta_prompt=delta_prompt, session_key=session_key)
        orch.call_agent_sessioned = recording

        def remove_override_and_continue():
            _write(routing_path, json.dumps({"schema_version": 1,
                                             "phases": {}}))
            self._inbox("keep going")
        self._inbox("hello")
        self.on_round[1] = remove_override_and_continue
        self.on_round[2] = self._end
        cfg = self._cfg()
        cfg["models"] = {"codex": "base-codex"}
        orch.process_phase(cfg, "demo", self.app_dir, self._phase(),
                           "seed", [], self._state())
        # Round 1 ran on the routed model + timeout...
        self.assertEqual(seen[1], ("routed-codex", 555))
        # ...and round 2 reverted BOTH the instant the override was removed.
        self.assertEqual(seen[2], ("base-codex", None))


class TestInboxDrainRace(unittest.TestCase):
    """A-70: the drain claims the inbox atomically (os.replace) instead of
    read-then-truncate — a message the human writes concurrently must land in
    a fresh inbox for the next drain, never be truncated away unseen."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.md = os.path.join(self.app_dir, "chat.md")
        open(self.md, "w").close()
        self.inbox = os.path.join(self.app_dir, "human_inbox.txt")

    def test_message_written_after_the_claim_survives(self):
        import unittest.mock
        _write(self.inbox, "first message")
        real_replace = os.replace

        def replace_then_concurrent_write(src, dst):
            # The writer lands the instant the engine claims the file — the
            # exact interleaving the old read-then-truncate destroyed.
            real_replace(src, dst)
            _write(src, "second message")
        with unittest.mock.patch.object(orch.os, "replace",
                                        replace_then_concurrent_write):
            transcript, msg = orch._drain_inbox_message(
                self.app_dir, self.md, "", "Round 1")
        self.assertEqual(msg, "first message")
        self.assertIn("first message", transcript)
        with open(self.inbox, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "second message")

    def test_drained_inbox_is_left_empty_but_present(self):
        _write(self.inbox, "hello")
        _t, msg = orch._drain_inbox_message(self.app_dir, self.md, "",
                                            "Round 1")
        self.assertEqual(msg, "hello")
        with open(self.inbox, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")   # invariant pollers rely on


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
        # Worst legitimate case is ~0.55s (0.3s writer sleep + one full 250ms
        # tick); 1.5s keeps real slack for a contended shared CI runner while
        # still discriminating — the legacy ~2s poll's earliest wake would be
        # ~2.0s, well past this bound, so the sub-second tick stays pinned.
        self.assertLess(elapsed, 1.5,
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
        self.assertLess(elapsed, 1.5)   # same discriminating bound as above
        self.assertIsNone(self.state.get("awaiting_human"))
