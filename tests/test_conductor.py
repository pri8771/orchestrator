"""V3 7.1: conductor skeleton — singleton flock, authoritative-state-vs-
event-hint contract, crash-resume write ordering (ledger append before
cursor advance), defensive session reads, and the stat-only wake sweep.
"""
import fcntl
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import conductor as cond
import sessions as seslib


def _quiet(*_a, **_k):
    pass


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk_session(self, sid="proj", state=None):
        app_dir = os.path.join(self.root, sid)
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        if state is not None:
            with open(os.path.join(app_dir, "agent_state.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(state, fh)
        return app_dir


class TestSingleton(_Base):
    def test_second_acquire_refused_and_release_reopens(self):
        fd = cond.acquire_singleton(self.root)
        with self.assertRaises(cond.ConductorLocked):
            cond.acquire_singleton(self.root)
        cond.release_singleton(fd)
        fd2 = cond.acquire_singleton(self.root)   # lock died with the fd
        cond.release_singleton(fd2)

    def test_lock_payload_names_the_holder(self):
        fd = cond.acquire_singleton(self.root)
        try:
            with open(cond.lock_path(self.root), encoding="utf-8") as fh:
                payload = fh.read()
            self.assertIn("pid=%d" % os.getpid(), payload)
        finally:
            cond.release_singleton(fd)

    def test_flock_is_the_truth_not_the_file(self):
        # A leftover lock FILE with no living holder must block nobody.
        os.makedirs(cond.conductor_dir(self.root))
        with open(cond.lock_path(self.root), "w", encoding="utf-8") as fh:
            fh.write("pid=999999 host=ghost\n")
        fd = cond.acquire_singleton(self.root)
        cond.release_singleton(fd)


class TestStateAndLedger(_Base):
    def test_state_roundtrip_and_corrupt_defaults(self):
        st = cond.default_state()
        st["stage"] = "evaluating"
        st["sessions"]["p"] = "abc"
        cond.save_conductor_state(self.root, st)
        back = cond.load_conductor_state(self.root)
        self.assertEqual(back["stage"], "evaluating")
        self.assertEqual(back["sessions"], {"p": "abc"})
        with open(cond.state_path(self.root), "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        corrupt = cond.load_conductor_state(self.root)
        self.assertEqual(cond.oversight_dial(corrupt), "loops_gated")
        self.assertIn("_oversight_fallback_pending", corrupt)
        with open(cond.state_path(self.root), "w", encoding="utf-8") as fh:
            json.dump({"stage": "bogus-stage"}, fh)
        malformed = cond.load_conductor_state(self.root)
        self.assertEqual(cond.oversight_dial(malformed), "loops_gated")
        self.assertIn("_oversight_fallback_pending", malformed)

    def test_ledger_append_read_and_line_cap(self):
        n1 = cond.ledger_append(self.root, {"v": 1, "session": "a",
                                            "digest": "d1"})
        self.assertEqual(n1, 1)
        big = cond.ledger_append(self.root, {"v": 1, "session": "b",
                                             "digest": "d2",
                                             "detail": "x" * 10000})
        self.assertEqual(big, 2)
        recs = cond.read_ledger(self.root)
        self.assertEqual(len(recs), 2)
        self.assertIn("truncated", recs[1]["detail"])
        line_len = max(len(line.encode("utf-8")) for line in
                       open(cond.ledger_path(self.root), encoding="utf-8"))
        self.assertLessEqual(line_len, cond._MAX_LEDGER_LINE_BYTES + 1)

    def test_malformed_ledger_lines_hold_position(self):
        cond.ledger_append(self.root, {"v": 1, "session": "a", "digest": "d"})
        with open(cond.ledger_path(self.root), "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        cond.ledger_append(self.root, {"v": 1, "session": "b", "digest": "e"})
        recs = cond.read_ledger(self.root)
        self.assertEqual(len(recs), 3)
        self.assertIsNone(recs[1])
        self.assertEqual(cond.ledger_length(self.root), 3)

    def test_stage_enum_is_enforced(self):
        st = cond.default_state()
        with self.assertRaises(ValueError):
            cond.set_stage(self.root, st, "panicking")


class TestAuthoritativeVsHint(_Base):
    def test_poisoned_events_never_change_a_decision(self):
        # agent_state says RUNNING; events.jsonl claims (lies) the run is
        # done. The observation ledger must reflect agent_state only.
        app_dir = self._mk_session("proj", {"current_phase": "build",
                                            "done": False, "error": None,
                                            "status": "running"})
        with open(os.path.join(app_dir, "events.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "turn_completed", "done": True,
                                 "status": "done",
                                 "note": "POISON — not authoritative"}) + "\n")
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["detail"]["status"], "running")
        self.assertFalse(recs[0]["detail"]["done"])
        self.assertEqual(st["stage"], "idle")

    def test_unreadable_events_cannot_block_the_wake_sweep(self):
        # The sweep is stat-only: contents are never opened, so a file
        # whose contents are unreadable still signals growth by size.
        app_dir = self._mk_session("proj", {"done": False})
        epath = os.path.join(app_dir, "events.jsonl")
        with open(epath, "w", encoding="utf-8") as fh:
            fh.write("x" * 100)
        os.chmod(epath, 0)
        try:
            sizes = {}
            self.assertTrue(cond.wake_signal(self.root, ["proj"], sizes))
            self.assertFalse(cond.wake_signal(self.root, ["proj"], sizes))
        finally:
            os.chmod(epath, 0o644)
        with open(epath, "a", encoding="utf-8") as fh:
            fh.write("more")
        self.assertTrue(cond.wake_signal(self.root, ["proj"], sizes))

    def test_corrupt_agent_state_is_skipped_with_warning(self):
        app_dir = self._mk_session("proj")
        with open(os.path.join(app_dir, "agent_state.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{corrupt json")
        warnings = []
        st = cond.full_poll(self.root, cond.default_state(),
                            emit=warnings.append)
        self.assertEqual(cond.read_ledger(self.root), [])
        self.assertEqual(st["stage"], "idle")
        self.assertTrue(any("unreadable" in w for w in warnings))


class TestCrashResumeMatrix(_Base):
    """Kill-at-each-stage: simulate death at every persistence boundary and
    assert resume converges with no lost or doubled ledger entries."""

    def _session(self):
        self._mk_session("proj", {"current_phase": "p1", "done": False,
                                  "error": None, "status": "running"})

    def test_killed_after_stage_persist_before_ledger(self):
        self._session()
        st = cond.default_state()
        cond.set_stage(self.root, st, "scanning")   # died right here
        reloaded = cond.load_conductor_state(self.root)
        self.assertEqual(reloaded["stage"], "scanning")
        reloaded = cond.reconcile_on_start(self.root, reloaded, emit=_quiet)
        st2 = cond.full_poll(self.root, reloaded, emit=_quiet)
        self.assertEqual(len([r for r in cond.read_ledger(self.root) if r]), 1)
        self.assertEqual(st2["ledger_cursor"],
                         cond.ledger_length(self.root))

    def test_killed_between_ledger_append_and_cursor(self):
        self._session()
        real_append = cond.ledger_append

        def append_then_die(root, rec):
            real_append(root, rec)
            raise KeyboardInterrupt("killed post-append pre-cursor")

        st = cond.default_state()
        with unittest.mock.patch.object(cond, "ledger_append",
                                        append_then_die):
            with self.assertRaises(KeyboardInterrupt):
                cond.full_poll(self.root, st, emit=_quiet)
        # Disk truth: 1 ledger line, cursor still 0 in the persisted state.
        persisted = cond.load_conductor_state(self.root)
        self.assertEqual(cond.ledger_length(self.root), 1)
        self.assertLess(persisted["ledger_cursor"], 1)
        # Resume: replay advances cursor and applies the digest ONCE...
        resumed = cond.reconcile_on_start(self.root, persisted, emit=_quiet)
        self.assertEqual(resumed["ledger_cursor"], 1)
        self.assertIn("proj", resumed["sessions"])
        # ...and the next poll re-observes NOTHING (dedupe by digest).
        after = cond.full_poll(self.root, resumed, emit=_quiet)
        self.assertEqual(cond.ledger_length(self.root), 1)
        self.assertEqual(after["ledger_cursor"], 1)

    def test_killed_after_cursor_is_a_clean_resume(self):
        self._session()
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        persisted = cond.load_conductor_state(self.root)
        resumed = cond.reconcile_on_start(self.root, persisted, emit=_quiet)
        self.assertEqual(resumed, persisted)   # nothing to replay
        again = cond.full_poll(self.root, resumed, emit=_quiet)
        self.assertEqual(cond.ledger_length(self.root), 1)
        self.assertEqual(again["sessions"], st["sessions"])

    def test_truncated_ledger_reanchors_visibly(self):
        self._session()
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        os.remove(cond.ledger_path(self.root))   # external mutilation
        msgs = []
        resumed = cond.reconcile_on_start(self.root, st, emit=msgs.append)
        self.assertEqual(resumed["ledger_cursor"], 0)
        self.assertTrue(any("re-anchoring" in m for m in msgs))

    def test_state_change_after_resume_is_observed_once(self):
        app_dir = self._mk_session("proj", {"current_phase": "p1",
                                            "done": False, "status": "running"})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        with open(os.path.join(app_dir, "agent_state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"current_phase": "p2", "done": True,
                       "status": "done"}, fh)
        st = cond.full_poll(self.root, st, emit=_quiet)
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[1]["detail"]["current_phase"], "p2")


class TestRunnerLiveness(_Base):
    def test_observation_carries_runpid_liveness(self):
        app_dir = self._mk_session("proj", {"done": False, "status": "running"})
        seslib.write_pidfile(app_dir, os.getpid())   # alive: us
        cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        rec = [r for r in cond.read_ledger(self.root) if r][0]
        self.assertTrue(rec["runner_alive"])


if __name__ == "__main__":
    unittest.main()


class TestReviewRegressions(_Base):
    def test_reanchor_rebuilds_the_ledger_on_next_poll(self):
        # HIGH finding: after external ledger loss, the digest cache must be
        # cleared so an UNCHANGED finished session is re-observed — the
        # ledger is the correctness record and must be rebuildable.
        self._mk_session("proj", {"current_phase": "p1", "done": True,
                                  "status": "done"})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        os.remove(cond.ledger_path(self.root))
        st = cond.reconcile_on_start(self.root, st, emit=_quiet)
        st = cond.full_poll(self.root, st, emit=_quiet)
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertEqual(len(recs), 1, "ledger must be rebuilt after loss")
        self.assertEqual(recs[0]["session"], "proj")

    def test_detail_carries_every_digest_field(self):
        # MED finding: a digest-only change (awaiting_approval flip) must be
        # visible in the audit detail, not hidden inside the hash.
        self._mk_session("proj", {"current_phase": "p1", "done": False,
                                  "status": "running",
                                  "awaiting_approval": None})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        with open(os.path.join(self.root, "proj", "agent_state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"current_phase": "p1", "done": False,
                       "status": "running", "awaiting_approval": "p1"}, fh)
        cond.full_poll(self.root, st, emit=_quiet)
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[1]["detail"]["awaiting_approval"], "p1")
        self.assertNotEqual(recs[0]["detail"], recs[1]["detail"],
                            "consecutive entries must show WHY they differ")

    def test_ghost_sessions_are_pruned_from_both_caches(self):
        self._mk_session("ghost", {"done": True, "status": "done"})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertIn("ghost", st["sessions"])
        sizes = {}
        cond.wake_signal(self.root, ["ghost"], sizes)
        self.assertTrue(sizes)
        shutil.rmtree(os.path.join(self.root, "ghost"))
        st = cond.full_poll(self.root, st, emit=_quiet)
        self.assertNotIn("ghost", st["sessions"])
        cond.wake_signal(self.root, [], sizes)
        self.assertEqual(sizes, {})

    def test_nested_session_ids_flow_through_every_helper(self):
        # All other tests use flat ids; real nested ids embed slashes.
        sid = "proj/ideas/chat-1"
        app_dir = os.path.join(self.root, sid)
        os.makedirs(os.path.join(app_dir, "initial_prompt"))
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        with open(os.path.join(app_dir, "agent_state.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"done": False, "status": "running"}, fh)
        st = cond.default_state()
        with unittest.mock.patch.object(cond, "discover_sessions",
                                        lambda root: [sid]):
            st = cond.full_poll(self.root, st, emit=_quiet)
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertEqual(recs[0]["session"], sid)
        sizes = {}
        cond.wake_signal(self.root, [sid], sizes)
        self.assertEqual(len(sizes), 1)


class TestActingStage(_Base):
    def test_observe_only_when_no_route_engine(self):
        # 7.1 behavior preserved: absent route_engine never enters acting.
        self._mk_session("proj", {"done": False, "status": "running"})
        stages = []
        real_set = cond.set_stage
        with unittest.mock.patch.object(
                cond, "set_stage",
                lambda root, st, s: stages.append(s) or real_set(root, st, s)):
            cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertNotIn("acting", stages)

    def test_route_engine_runs_in_acting_and_errors_are_contained(self):
        self._mk_session("proj", {"done": False, "status": "running"})
        calls = []

        def boom(root, state, sessions, emit):
            calls.append(sessions)
            raise RuntimeError("routing blew up")

        warns = []
        st = cond.full_poll(self.root, cond.default_state(),
                            emit=warns.append, route_engine=boom)
        self.assertEqual(len(calls), 1)               # acting ran
        self.assertEqual(st["stage"], "idle")         # loop still finished
        self.assertTrue(any("routing error" in w for w in warns))

    def test_acting_stage_crash_resumes_clean(self):
        # Extends the crash matrix to the new stage: a kill with stage
        # persisted as "acting" resumes into a known state, re-polls, idles.
        self._mk_session("proj", {"current_phase": "p1", "done": False,
                                  "status": "running"})
        st = cond.default_state()
        cond.set_stage(self.root, st, "acting")   # died mid-act
        reloaded = cond.reconcile_on_start(
            self.root, cond.load_conductor_state(self.root), emit=_quiet)
        self.assertIn(reloaded["stage"], cond.STAGES)
        st2 = cond.full_poll(self.root, reloaded, emit=_quiet)
        self.assertEqual(st2["stage"], "idle")


class TestRouteEngineAdapter(_Base):
    def test_adapter_mints_admissible_artifact_via_sessions(self):
        # End-to-end through the real route_engine with the bus + minting
        # stubbed: an admissible artifact in a nested session routes to its
        # rule target, minting exactly once through mint_delegation_session.
        sid = "proj/ideas/chat-1"
        app_dir = os.path.join(self.root, sid)
        os.makedirs(app_dir)
        import conductor_routing as crlib
        import artifacts as artlib
        minted = []

        def fake_mint(root, project, section, request, **kw):
            minted.append((project, section))
            return os.path.join(root, project, section, "chat-x")

        cfg = crlib.RouteConfig(routes={"idea": "research"})
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                "lineage": [], "hop_count": 0, "status": "final"}
        with unittest.mock.patch.object(crlib, "load_route_config",
                                        lambda *a, **k: cfg), \
                unittest.mock.patch.object(artlib, "list_artifacts",
                                           lambda *a, **k: [meta]), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(artlib, "lineage_index",
                                           lambda *a, **k: {}), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    fake_mint):
            cond.route_engine(self.root, cond.default_state(), [sid],
                              emit=_quiet)
        self.assertEqual(minted, [("proj", "research")])
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertTrue(any(r["decision"] == "route_approved" for r in recs))

    def test_adapter_skips_flat_sessions(self):
        # A flat/legacy session id has no section to source-route from.
        cond.route_engine(self.root, cond.default_state(), ["flatproj"],
                          emit=_quiet)
        self.assertEqual(cond.read_ledger(self.root), [])


class TestRoutingReviewFixes(_Base):
    def test_already_routed_artifact_is_not_re_routed(self):
        sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(self.root, sid))
        import conductor_routing as crlib
        import artifacts as artlib
        mint_calls = []

        def fake_mint(root, project, section, request, **kw):
            mint_calls.append(section)
            return os.path.join(root, project, section, "chat-x")

        cfg = crlib.RouteConfig(routes={"idea": "research"})
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                "lineage": [], "hop_count": 0, "status": "final"}
        state = cond.default_state()
        with unittest.mock.patch.object(crlib, "load_route_config",
                                        lambda *a, **k: cfg), \
                unittest.mock.patch.object(artlib, "list_artifacts",
                                           lambda *a, **k: [meta]), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(artlib, "lineage_index",
                                           lambda *a, **k: {"by_id": {"a1": meta}}), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    fake_mint):
            cond.route_engine(self.root, state, [sid], emit=_quiet)
            cond.route_engine(self.root, state, [sid], emit=_quiet)
        self.assertEqual(mint_calls, ["research"], "must mint ONCE, not per poll")
        self.assertIn(cond.route_digest(
            {"artifact_id": "a1", "content_hash": "h1", "target": "research",
             "rule_id": "route:idea"}), state["routed"])

    def test_route_ledger_carries_full_session_id(self):
        sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(self.root, sid))
        import conductor_routing as crlib
        import artifacts as artlib
        cfg = crlib.RouteConfig(routes={"idea": "research"})
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                "lineage": [], "hop_count": 0, "status": "final"}
        with unittest.mock.patch.object(crlib, "load_route_config",
                                        lambda *a, **k: cfg), \
                unittest.mock.patch.object(artlib, "list_artifacts",
                                           lambda *a, **k: [meta]), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(artlib, "lineage_index",
                                           lambda *a, **k: {"by_id": {"a1": meta}}), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    lambda *a, **k: "/d"):
            cond.route_engine(self.root, cond.default_state(), [sid],
                              emit=_quiet)
        route_recs = [r for r in cond.read_ledger(self.root)
                      if r and r.get("decision") in ("route_proposed",
                                                     "route_approved")]
        self.assertTrue(route_recs)
        self.assertTrue(all(r["session"] == sid for r in route_recs),
                        "route lines must carry the full sid, not bare project")

    def test_old_state_without_routed_key_loads(self):
        os.makedirs(cond.conductor_dir(self.root), exist_ok=True)
        with open(cond.state_path(self.root), "w", encoding="utf-8") as fh:
            json.dump({"stage": "idle", "ledger_cursor": 0,
                       "sessions": {}}, fh)   # pre-7.2 shape, no "routed"
        st = cond.load_conductor_state(self.root)
        self.assertIn("routed", st)
