"""V3 board 2.4: messages.jsonl — the machine-readable transcript index.

Covers the sink contract (schema, ids, never-raise, redaction, cap, the
message_appended event), reconciliation (round-scoped + final-stage drops,
atomicity, corrupt-line handling), thread interleaving, and the two
integration invariants the card names: crash-resume produces no duplicate
and no orphaned lines, and the kill window between the .md truncate and
the jsonl rewrite converges on the next resume.
"""
import datetime
import json
import os
import tempfile
import threading
import unittest

import events as evlib
import messages as msglib
import orchestrator as orch
import workflows as wf


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class SinkBase(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_msgs_")
        self.md = os.path.join(self.app_dir, "design", "d.md")
        _write(self.md, "# header\n")

    def append(self, **kw):
        base = dict(phase="design", kind="turn", author="codex",
                    md_path=self.md, rnd=1)
        base.update(kw)
        return msglib.append_message(self.app_dir, base.pop("phase"),
                                     base.pop("kind"), base.pop("author"),
                                     base.pop("md_path"), **base)


class TestAppendSchema(SinkBase):
    def test_line_has_exactly_the_contract_fields(self):
        self.assertTrue(self.append(persona="Skeptic"))
        (line,) = msglib.read_messages(self.app_dir)
        self.assertEqual(set(line), {"turn_id", "phase", "kind", "agent",
                                     "role", "persona", "round", "ts",
                                     "content_path"})
        self.assertEqual(line["turn_id"], "design:1:codex:turn")
        self.assertEqual(line["agent"], "codex")
        self.assertEqual(line["round"], 1)
        # tz-aware isoformat parses and carries an offset
        ts = datetime.datetime.fromisoformat(line["ts"])
        self.assertIsNotNone(ts.tzinfo)
        # content_path is app_dir-relative and resolves to the real .md
        self.assertFalse(os.path.isabs(line["content_path"]))
        self.assertTrue(os.path.exists(
            os.path.join(self.app_dir, line["content_path"])))

    def test_no_body_field_ever(self):
        self.append()
        (line,) = msglib.read_messages(self.app_dir)
        self.assertFalse({"content", "body", "text"} & set(line))

    def test_turn_id_slot_seq_and_token_variants(self):
        self.append(kind="human", author="human", slot="coord")
        self.append(kind="retry", rnd=3, seq=2)
        self.append(kind="tally", author="orchestrator", token="final", rnd=0)
        ids = [m["turn_id"] for m in msglib.read_messages(self.app_dir)]
        self.assertEqual(ids, ["design:1:human:human.coord",
                               "design:3:codex:retry:2",
                               "design:final:orchestrator:tally"])

    def test_never_raises_and_returns_false(self):
        bad = os.path.join(self.app_dir, "not-a-dir-file")
        _write(bad, "x")   # a FILE where a dir is expected
        self.assertFalse(msglib.append_message(
            os.path.join(bad, "nope"), "p", "turn", "codex", self.md))
        self.assertFalse(msglib.append_message(
            self.app_dir, "", "turn", "codex", self.md))

    def test_string_fields_are_redacted(self):
        self.append(persona="key sk-ant-api03-" + "a" * 40)
        (line,) = msglib.read_messages(self.app_dir)
        self.assertNotIn("sk-ant-api03-", line["persona"])

    def test_line_stays_small(self):
        self.append(persona="p" * 5000, role="r" * 5000)
        with open(msglib.messages_path(self.app_dir), encoding="utf-8") as fh:
            raw = fh.readline()
        self.assertLessEqual(len(raw.encode("utf-8")), 3500)

    def test_each_append_emits_one_message_appended_event(self):
        self.append()
        self.append(rnd=2)
        evts = evlib.read_events(self.app_dir, kinds=["message_appended"])
        self.assertEqual(len(evts), 2)
        for e in evts:
            self.assertIn("turn_id", e)
            self.assertIn("content_path", e)
            self.assertFalse({"content", "body"} & set(e))

    def test_threaded_appends_do_not_interleave(self):
        def worker(i):
            for j in range(50):
                self.append(author="lane%d" % i, rnd=j)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = msglib.read_messages(self.app_dir)
        self.assertEqual(len(lines), 400, "every line must parse cleanly")


class TestReconcile(SinkBase):
    def _seed(self):
        for rnd in (1, 2, 3):
            self.append(rnd=rnd)
            self.append(kind="human", author="human", slot="open", rnd=rnd)
        self.append(kind="vote", token="final", rnd=0)
        self.append(kind="tally", author="orchestrator", token="final", rnd=0)
        self.append(phase="research", kind="turn", rnd=9)

    def test_resume_drops_rounds_but_keeps_post_round_kinds(self):
        # Mirrors _resume_round_state: a resume-truncate only ever cuts a
        # trailing incomplete round; the .md KEEPS repair/vote/tally
        # blocks, so their lines must survive.
        self._seed()
        self.assertTrue(msglib.reconcile_messages(self.app_dir, "design",
                                                  keep_below_round=3))
        lines = msglib.read_messages(self.app_dir)
        design = [m for m in lines if m["phase"] == "design"]
        rounds = {m["round"] for m in design
                  if m["kind"] not in msglib.POST_ROUND_KINDS}
        self.assertEqual(rounds, {1, 2})
        self.assertEqual(
            sorted(m["kind"] for m in design
                   if m["kind"] in msglib.POST_ROUND_KINDS),
            ["tally", "vote"],
            "post-round lines must survive a resume reconcile")
        # the other phase is untouched
        self.assertTrue([m for m in lines if m["phase"] == "research"])

    def test_drop_all_for_fresh_start(self):
        self._seed()
        msglib.reconcile_messages(self.app_dir, "design", keep_below_round=1,
                                  drop_post_round=True)
        lines = msglib.read_messages(self.app_dir)
        self.assertFalse([m for m in lines if m["phase"] == "design"])
        self.assertEqual(len(lines), 1)

    def test_missing_file_and_nothing_to_drop_are_fine(self):
        self.assertTrue(msglib.reconcile_messages(self.app_dir, "design", 1))
        self.append(rnd=1)
        self.assertTrue(msglib.reconcile_messages(self.app_dir, "other", 1))
        self.assertEqual(len(msglib.read_messages(self.app_dir)), 1)

    def test_one_bad_round_value_does_not_abort_reconciliation(self):
        self._seed()
        with open(msglib.messages_path(self.app_dir), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"turn_id": "design:x:codex:turn",
                                 "phase": "design", "kind": "turn",
                                 "round": "not-a-number"}) + "\n")
        self.assertTrue(msglib.reconcile_messages(self.app_dir, "design",
                                                  keep_below_round=2))
        lines = msglib.read_messages(self.app_dir)
        design_turns = [m for m in lines if m["phase"] == "design"
                        and m["kind"] == "turn"]
        self.assertEqual({m["round"] for m in design_turns},
                         {1, "not-a-number"},
                         "round >= 2 dropped; the malformed line kept")

    def test_non_utf8_byte_costs_one_line_not_the_read(self):
        self.append(rnd=1)
        with open(msglib.messages_path(self.app_dir), "ab") as fh:
            fh.write(b'{"turn_id": "\xff\xfe"}\n')
        self.append(rnd=2)
        lines = msglib.read_messages(self.app_dir)
        self.assertEqual([m["round"] for m in lines if "round" in m], [1, 2])


class TestPostRoundDedupe(SinkBase):
    def test_second_vote_section_gets_suffixed_ids(self):
        self.append(kind="vote", token="final", rnd=0)
        self.append(kind="vote", token="final", rnd=0)   # resume re-run
        self.append(kind="vote", token="final", rnd=0)
        ids = [m["turn_id"] for m in msglib.read_messages(self.app_dir)]
        self.assertEqual(ids, ["design:final:codex:vote",
                               "design:final:codex:vote:2",
                               "design:final:codex:vote:3"])

    def test_repair_restart_does_not_collide(self):
        self.append(kind="repair", rnd=1)
        self.append(kind="repair", rnd=2)
        # crash-resume: _verify_and_repair restarts attempt numbering at 1
        self.append(kind="repair", rnd=1)
        ids = [m["turn_id"] for m in msglib.read_messages(self.app_dir)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("design:1:codex:repair:2", ids)

    def test_round_kinds_do_not_pay_the_dedupe_scan(self):
        # Round-scoped ids are made unique by reconciliation, not scanning;
        # a plain turn re-append keeps its raw id (documented model).
        self.append(rnd=1)
        self.append(rnd=1)
        ids = [m["turn_id"] for m in msglib.read_messages(self.app_dir)]
        self.assertEqual(ids, ["design:1:codex:turn", "design:1:codex:turn"])


class ResumeBase(unittest.TestCase):
    """Drive process_phase with stub agents (test_roster_turns pattern)."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_msgs_resume_")
        self._orig_sessioned = orch.call_agent_sessioned
        self._orig_agent = orch.call_agent
        self._orig_avail = orch._agent_available

    def tearDown(self):
        orch.call_agent_sessioned = self._orig_sessioned
        orch.call_agent = self._orig_agent
        orch._agent_available = self._orig_avail

    def _cfg(self):
        return {"agents": {"codex_enabled": True, "claude_enabled": False,
                           "gemini_enabled": False},
                "runtime": {"parallel_discussion_rounds": False,
                            "phase_quality_gates_enabled": False,
                            "phase_independent_first_round_enabled": False,
                            "approval_timeout_seconds": 30},
                "_workflow_target": "app", "_app_dir": self.app_dir,
                "root": self.app_dir}

    def _state(self, rnd=0):
        return {"current_phase": "design_discussion" if rnd else None,
                "current_round": rnd, "completed_phases": [],
                "phase_outputs": {}, "consensus_status": {},
                "vote_results": {}, "prompt_hash": "h"}

    def _stub(self, consensus_at=2):
        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            if (session_key or "").endswith(":coord"):
                if rnd >= consensus_at:
                    return "Done.\n\nCONSENSUS: YES\n\n## Final Output\n\nX.\n"
                return "go on. CONSENSUS: NO"
            return "codex take round %d" % rnd
        orch.call_agent_sessioned = sessioned
        orch._agent_available = lambda a, cfg=None: a == "codex"

    def _run(self, state):
        return orch.process_phase(
            self._cfg(), "r", self.app_dir,
            wf.Phase("design_discussion", ".", "d.md", "p", rounds=3),
            "p", [], state)

    def _md_text(self):
        with open(os.path.join(self.app_dir, "d.md"), encoding="utf-8") as fh:
            return fh.read()


class TestRunProducesIndex(ResumeBase):
    def test_completed_run_indexes_every_authored_block(self):
        # The consistency invariants (no dupes/orphans) hold vacuously if
        # a site is unwired — this pins COMPLETENESS: a two-round debate
        # must index each roster turn and each coordinator decision.
        self._stub(consensus_at=2)
        self._run(self._state())
        by_kind = {}
        for m in msglib.read_messages(self.app_dir):
            by_kind.setdefault(m["kind"], []).append(m["turn_id"])
        self.assertEqual(sorted(by_kind.get("turn", [])),
                         ["design_discussion:1:codex:turn",
                          "design_discussion:2:codex:turn"])
        self.assertEqual(sorted(by_kind.get("coordinator", [])),
                         ["design_discussion:1:codex:coordinator",
                          "design_discussion:2:codex:coordinator"])

    def test_human_drain_indexes_with_slot(self):
        self._stub(consensus_at=1)
        _write(os.path.join(self.app_dir, "human_inbox.txt"), "note")
        self._run(self._state())
        humans = [m for m in msglib.read_messages(self.app_dir)
                  if m["kind"] == "human"]
        self.assertTrue(humans, "the drained inbox message must be indexed")
        self.assertTrue(all(":human:human." in m["turn_id"] for m in humans),
                        "human ids must carry a structural slot")


class TestCrashResume(ResumeBase):
    def test_resume_yields_no_dupes_and_no_orphans(self):
        self._stub(consensus_at=2)
        self._run(self._state())
        # Simulate a crash mid-round-3: a partial round in the .md and its
        # already-indexed lines in messages.jsonl.
        md = os.path.join(self.app_dir, "d.md")
        with open(md, "a", encoding="utf-8") as fh:
            fh.write("\n### Round 3\n\n**Codex — Round 3**\n\npartial\n")
        msglib.append_message(self.app_dir, "design_discussion", "turn",
                              "codex", md, rnd=3)
        state = self._state(rnd=3)
        self._run(state)
        lines = msglib.read_messages(self.app_dir)
        ids = [m["turn_id"] for m in lines]
        self.assertEqual(len(ids), len(set(ids)), "duplicate turn_ids: %s"
                         % sorted(set(i for i in ids if ids.count(i) > 1)))
        text = self._md_text()
        for m in lines:
            if m["kind"] in msglib.POST_ROUND_KINDS:
                continue   # attempt/final keyspace — not round headers
            self.assertIn("Round %d" % m["round"], text,
                          "orphan line for a round the .md dropped: %s"
                          % m["turn_id"])

    def test_kill_between_md_truncate_and_jsonl_rewrite_converges(self):
        self._stub(consensus_at=2)
        self._run(self._state())
        md = os.path.join(self.app_dir, "d.md")
        with open(md, "a", encoding="utf-8") as fh:
            fh.write("\n### Round 3\n\n**Codex — Round 3**\n\npartial\n")
        msglib.append_message(self.app_dir, "design_discussion", "turn",
                              "codex", md, rnd=3)
        # First resume "crashes" in the window: md truncated, jsonl NOT
        # reconciled (reconcile stubbed to a no-op), then the run dies
        # before any turn (call stub raises).
        orig_reconcile = orch.msglib.reconcile_messages

        def boom(*a, **k):
            raise KeyboardInterrupt

        orch.msglib.reconcile_messages = lambda *a, **k: True   # window: skipped
        orch.call_agent_sessioned = boom
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._run(self._state(rnd=3))
        finally:
            orch.msglib.reconcile_messages = orig_reconcile
        # jsonl is now STALE (round-3 line survives; md dropped round 3).
        stale = [m for m in msglib.read_messages(self.app_dir)
                 if m["round"] == 3]
        self.assertTrue(stale, "precondition: the window left a stale line")
        # Second resume runs both steps -> converges.
        self._stub(consensus_at=2)
        self._run(self._state(rnd=3))
        lines = msglib.read_messages(self.app_dir)
        ids = [m["turn_id"] for m in lines]
        self.assertEqual(len(ids), len(set(ids)))
        text = self._md_text()
        for m in lines:
            if m["kind"] not in msglib.POST_ROUND_KINDS:
                self.assertIn("Round %d" % m["round"], text)

    def _run_two_agent_vote_phase(self, state):
        # A no-consensus, two-agent debate -> forced vote. The unparseable
        # "ballot prose" drives the LLM-fallback tally (< 2 parseable ballots);
        # the tally decides YES.
        def sessioned(cfg, app, phase, rnd, agent, prompt,
                      delta_prompt=None, session_key=None):
            return "go on. CONSENSUS: NO"
        def voter(cfg, app, phase, rnd, agent, prompt):
            return ("ballot prose" if rnd == "vote"
                    else "tally prose.\n\nVOTE_DECISION: YES")
        orch.call_agent_sessioned = sessioned
        orch.call_agent = voter
        orch._agent_available = lambda a, cfg=None: a in ("codex", "claude")
        cfg = self._cfg()
        cfg["agents"]["claude_enabled"] = True
        orch.process_phase(cfg, "r", self.app_dir,
                           wf.Phase("design_discussion", ".", "d.md", "p",
                                    rounds=2),
                           "p", [], state)

    def test_vote_crash_resume_recovers_decision_without_re_voting(self):
        # Formerly test_vote_crash_resume_keeps_old_lines_and_suffixes_new: the
        # v1 harness that PROVED the double-vote bug (the .md kept the first
        # Forced Vote section — all rounds complete, the kept segment runs to
        # EOF — and the resumed run appended a SECOND one). Now pins the FIX:
        # a completed forced vote is RECOVERED on resume, the footer is written
        # once, and the vote is NOT re-run. Two agents: a vote never runs solo.
        self._run_two_agent_vote_phase(self._state())
        first = msglib.read_messages(self.app_dir)
        first_votes = [m for m in first
                       if m["kind"] in msglib.FINAL_STAGE_KINDS]
        self.assertTrue(first_votes, "precondition: the vote was indexed")
        md_after_first = self._md_text()
        self.assertEqual(md_after_first.count("### Forced Vote"), 1)
        # crash after the vote but before completion: resumable state (the .md
        # is a complete transcript incl. footer; only current_round was not
        # flushed to 0 — exactly the seam _resume_round_state must reconcile).
        self._run_two_agent_vote_phase(self._state(rnd=2))
        lines = msglib.read_messages(self.app_dir)
        ids = [m["turn_id"] for m in lines]
        self.assertEqual(len(ids), len(set(ids)),
                         "no duplicate turn_ids: the vote must not be re-run")
        second_votes = [m for m in lines
                        if m["kind"] in msglib.FINAL_STAGE_KINDS]
        self.assertEqual(len(second_votes), len(first_votes),
                         "exactly ONE vote section — no re-vote, no second set")
        for m in first_votes:
            self.assertIn(m["turn_id"], ids,
                          "the completed vote's index lines are kept, not dropped")
        text = self._md_text()
        self.assertEqual(text.count("### Forced Vote"), 1,
                         "one Forced Vote section survives the resume")
        self.assertEqual(text.count("## Coordinator Decision"), 1,
                         "one phase footer — the resume did not double it")
        self.assertEqual(text, md_after_first,
                         "resume is byte-idempotent for a completed vote")

    def test_partial_forced_vote_crash_resume_recasts_once(self):
        # A crash MID-VOTE (the "### Forced Vote" header + ballots reached disk
        # but the tally did not) leaves a PARTIAL section. Resume must discard
        # it — and its now-orphaned index lines — and cast the vote cleanly:
        # exactly one section, no duplicate/orphan turn_ids.
        self._run_two_agent_vote_phase(self._state())
        first_votes = [m for m in msglib.read_messages(self.app_dir)
                       if m["kind"] in msglib.FINAL_STAGE_KINDS]
        self.assertTrue(first_votes, "precondition: the vote was indexed")
        # Truncate the .md to header+ballots (drop the tally block + footer);
        # the messages.jsonl still carries the stale tally line — the crash seam.
        md = os.path.join(self.app_dir, "d.md")
        full = self._md_text()
        partial = full[:full.rindex("\n**Coordinator (")]   # last == the tally block
        self.assertIn("### Forced Vote", partial)
        self.assertNotIn("vote tally & decision", partial)
        with open(md, "w", encoding="utf-8") as fh:
            fh.write(partial)
        self._run_two_agent_vote_phase(self._state(rnd=2))
        lines = msglib.read_messages(self.app_dir)
        ids = [m["turn_id"] for m in lines]
        self.assertEqual(len(ids), len(set(ids)),
                         "the partial section's stale lines are dropped, re-cast is clean")
        text = self._md_text()
        self.assertEqual(text.count("### Forced Vote"), 1,
                         "the partial section was discarded and re-cast once")
        self.assertEqual(text.count("## Coordinator Decision"), 1)
        second_votes = [m for m in lines
                        if m["kind"] in msglib.FINAL_STAGE_KINDS]
        self.assertEqual(len(second_votes), len(first_votes),
                         "one vote section's worth of index lines after re-cast")


if __name__ == "__main__":
    unittest.main()
