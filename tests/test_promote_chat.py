"""V3 board 1.8: 'Let them discuss' — promote a chat session to an auto
debate in the same dir.

The seam under test: the transcript is read from chat/chat.md (the source of
truth — phase_outputs deliberately holds only a closure note) and carried in
carryover_outputs under a key no workflow declares, which run()'s prior-
output assembly always injects. The integration test drives process_phase
with prior_outputs built exactly the way run() builds them from carryover,
and asserts the chat content reaches the stub agents' prompts.
"""
import os
import tempfile
import unittest

import orchestrator as orch
import workflows as wf


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


CHAT_MD = """# home--ideas--demo — Ideas Chat

## Original Prompt

```
Brainstorm a weekend-sized iOS utility.
```

## Transcript

### Round 1

**You (human) — Round 1**

What about a tide-clock widget for surfers?

**Codex (Pragmatist) — Round 1**

Tide data is free from NOAA; a widget-only app is genuinely weekend-sized.

## Coordinator Decision

_No coordinator — conversational phase; ended by user._

## Final Output

Conversation closed after 1 round(s): ended by user.

---

ENDED BY USER
"""


class PromoteBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.slug = "home--ideas--demo"
        self.app_dir = os.path.join(self.root, self.slug)
        _write(os.path.join(self.app_dir, "initial_prompt", "initial_prompt.md"),
               "Brainstorm a weekend-sized iOS utility.")
        _write(os.path.join(self.app_dir, "workflow.txt"), "chat_ideas\n")
        _write(os.path.join(self.app_dir, "chat", "chat.md"), CHAT_MD)
        # State as the conversational finalizer leaves it: closure note only.
        st = {"current_phase": None, "current_round": 0, "next_agent": None,
              "workflow": "chat_ideas",
              "completed_phases": ["chat"], "prompt_hash": "h",
              "phase_outputs": {"chat": "Conversation closed after 1 round(s): "
                                        "ended by user."},
              "consensus_status": {"chat": False}, "vote_results": {},
              "conversation_end": {"chat": "ended by user"},
              "done": True, "error": None}
        orch.save_state(self.app_dir, st)

    def _promote(self, to=None, wait=1):
        return orch.promote_chat(self.root, self.slug, to_workflow=to,
                                 wait_seconds=wait)

    def _state(self):
        return orch.load_state(self.app_dir)


class TestPromoteChat(PromoteBase):
    def test_promotion_surgery(self):
        rc, target = self._promote()
        self.assertEqual((rc, target), (0, self.slug))
        st = self._state()
        # The transcript itself was carried — not the closure note.
        self.assertIn("tide-clock widget", st["carryover_outputs"]["chat_transcript"])
        self.assertEqual(st["workflow"], "brainstorm")
        self.assertFalse(st["done"])
        self.assertIsNone(st.get("awaiting_human"))
        self.assertEqual(st["promoted_from_chat"]["workflow"], "chat_ideas")
        with open(os.path.join(self.app_dir, "workflow.txt"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "brainstorm")
        # completed_phases untouched for keys the target doesn't share; the
        # chat phase must NOT block the brainstorm phases from running.
        bw = wf.load_workflow("brainstorm")
        for p in bw.phases:
            self.assertNotIn(p.key, st["completed_phases"])

    def test_explicit_target_override(self):
        rc, _ = self._promote(to="research")
        self.assertEqual(rc, 0)
        self.assertEqual(self._state()["workflow"], "research")

    def test_double_promotion_is_a_safe_noop(self):
        self.assertEqual(self._promote()[0], 0)
        st_before = self._state()
        rc, target = self._promote()
        self.assertEqual((rc, target), (0, None))   # message, no re-run
        st_after = self._state()
        self.assertEqual(st_before["carryover_outputs"],
                         st_after["carryover_outputs"])
        self.assertEqual(st_before["promoted_from_chat"],
                         st_after["promoted_from_chat"])

    def test_non_chat_project_is_refused(self):
        _write(os.path.join(self.app_dir, "workflow.txt"), "app_build\n")
        st = self._state()
        st.pop("promoted_from_chat", None)
        orch.save_state(self.app_dir, st)
        rc, target = self._promote()
        self.assertEqual((rc, target), (2, None))

    def test_conversational_target_is_refused(self):
        rc, target = self._promote(to="chat_research")
        self.assertEqual((rc, target), (2, None))
        self.assertEqual(self._state()["workflow"], "chat_ideas",
                         "refusal must not half-edit state")

    def test_live_session_waits_then_reports_busy(self):
        # A live engine lock (our own pid = definitely alive): promotion must
        # queue the end command, wait its bound, and refuse — never do state
        # surgery under a live engine.
        lockp = orch._app_lock_path(self.slug)
        _write(lockp, "pid=%d host=test started=now\n" % os.getpid())
        try:
            rc, target = self._promote(wait=1)
            self.assertEqual((rc, target), (3, None))
            self.assertTrue(os.path.exists(os.path.join(
                self.app_dir, "approvals", "chat.ok")),
                "the end command stays queued for the live engine")
            self.assertEqual(self._state()["workflow"], "chat_ideas")
            self.assertTrue(self._state()["done"], "no surgery happened")
        finally:
            os.remove(lockp)

    def test_crashed_chat_promotes_what_is_on_disk(self):
        st = self._state()
        st["done"] = False
        st["current_phase"] = "chat"
        st["current_round"] = 1
        st["awaiting_human"] = "chat"   # stale marker from a kill -9
        orch.save_state(self.app_dir, st)
        rc, _ = self._promote()
        self.assertEqual(rc, 0)
        st = self._state()
        self.assertIn("tide-clock widget", st["carryover_outputs"]["chat_transcript"])
        self.assertIsNone(st["awaiting_human"])

    def test_carryover_reaches_the_auto_agents_prompts(self):
        # End-to-end seam check: after promotion, drive a debate phase with
        # prior_outputs assembled EXACTLY the way run() assembles them from
        # carryover_outputs, and assert the chat content reaches the agents.
        self.assertEqual(self._promote()[0], 0)
        st = self._state()
        phases = wf.load_workflow("brainstorm").phases
        cur_keys = {p.key for p in phases}
        prior = [(ck, cv) for ck, cv in st["carryover_outputs"].items()
                 if cv and ck not in cur_keys]
        self.assertTrue(any("tide-clock" in v for _k, v in prior))

        prompts = []
        orig_sess = orch.call_agent_sessioned
        orig_avail = orch._agent_available
        orch._agent_available = lambda agent, cfg=None: agent == "codex"

        def fake_sessioned(cfg, app, phase, rnd, agent, prompt,
                           delta_prompt=None, session_key=None):
            prompts.append(prompt)
            if (session_key or "").endswith(":coord"):
                return "OK. CONSENSUS: YES\n\n## Final Output\n\nGo.\n"
            return "debate take"
        orch.call_agent_sessioned = fake_sessioned
        try:
            cfg = {"agents": {"codex_enabled": True, "claude_enabled": False,
                              "gemini_enabled": False},
                   "runtime": {"parallel_discussion_rounds": False,
                               "phase_quality_gates_enabled": False,
                               "phase_independent_first_round_enabled": False},
                   "_workflow_target": "app", "_app_dir": self.app_dir,
                   "root": self.root}
            run_state = {"current_phase": None, "current_round": 0,
                         "completed_phases": [], "phase_outputs": {},
                         "consensus_status": {}, "vote_results": {},
                         "prompt_hash": "h"}
            phase = wf.Phase("debate", ".", "debate.md", "debate it", rounds=2)
            orch.process_phase(cfg, self.slug, self.app_dir, phase,
                               "seed", prior, run_state)
        finally:
            orch.call_agent_sessioned = orig_sess
            orch._agent_available = orig_avail
        self.assertTrue(any("tide-clock widget" in p for p in prompts),
                        "the chat transcript must reach the debate agents")


if __name__ == "__main__":
    unittest.main()
