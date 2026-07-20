"""V3 7.5(a): goal-predicate + quiescence termination matrix.

Covers the acceptance criteria for the two layers this sub-PR lands. Where a
behavior depends on how the AUTHORITATIVE artifact store is queried (open gaps,
genuine live finals, oscillation), the tests drive a REAL on-disk store via
artifacts.publish — never a kwarg-ignoring mock — so a regression that widened
a status/type filter (the failure class an earlier card shipped) is actually
caught. evalharness is the one legitimate external leaf that is patched.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import artifacts as artlib
import conductor as cond
import conductor_termination as ct
import events as evlib


def _quiet(*_a, **_k):
    pass


class _Store:
    """A real on-disk artifact store rooted at app_dir (seed types, no mocks)."""

    def __init__(self, app_dir):
        self.app_dir = app_dir
        self.errors = []
        self.reg = artlib.load_registry(os.path.join(app_dir, ".orch"),
                                        on_error=self.errors.append)

    def publish(self, meta, body="body\n", consensus=True, supersedes=None):
        return artlib.publish(self.app_dir, body, meta, self.reg,
                              on_error=self.errors.append, consensus=consensus,
                              supersedes=supersedes)

    def gap(self, title="Missing X", consensus=True):
        return self.publish({"type": "gap", "title": title,
                             "fields": {"impact": "high"}}, consensus=consensus)

    def idea(self, title, body, consensus=True, supersedes=None):
        return self.publish({"type": "idea", "title": title}, body=body,
                            consensus=consensus, supersedes=supersedes)


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _app(self, sid="proj/documentation/chat-1"):
        app_dir = os.path.join(self.root, sid)
        os.makedirs(app_dir, exist_ok=True)
        return app_dir

    def _gap_report(self, app_dir):
        os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
        with open(os.path.join(app_dir, "docs", "GAP_REPORT.md"), "w") as fh:
            fh.write("None — every blueprint slot is filled.\n")

    def _adherence(self, app_dir, verdict="PASS"):
        os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
        with open(os.path.join(app_dir, "docs", "adherence.json"), "w") as fh:
            json.dump({"verdict": verdict, "score": 88}, fh)

    def _manifest(self, obj):
        with open(os.path.join(self.root, "goal_manifest.json"), "w") as fh:
            json.dump(obj, fh)

    def _patch_eval(self, **score):
        import evalharness
        self._orig_eval = evalharness.score_project
        evalharness.score_project = lambda *_a, **_k: score
        self.addCleanup(self._restore_eval)

    def _restore_eval(self):
        import evalharness
        evalharness.score_project = self._orig_eval


# --------------------------------------------------------------------------- #
# Manifest loading / validation
# --------------------------------------------------------------------------- #
class TestManifestLoad(_Base):
    def test_missing_manifest_is_safe_default(self):
        m = ct.load_goal_manifest(self.root, on_warn=_quiet)
        self.assertIsNone(m["goal"])
        self.assertIsNone(m["quiescence_cycles"])

    def test_corrupt_manifest_is_safe_default_with_banner(self):
        with open(os.path.join(self.root, "goal_manifest.json"), "w") as fh:
            fh.write("{not json")
        warned = []
        m = ct.load_goal_manifest(self.root, on_warn=warned.append)
        self.assertIsNone(m["goal"])
        self.assertTrue(any("unreadable" in w for w in warned))

    def test_invalid_goal_type_disabled(self):
        self._manifest({"goal": "done please"})
        self.assertIsNone(ct.load_goal_manifest(self.root, on_warn=_quiet)["goal"])

    def test_unknown_tier_dropped(self):
        self._manifest({"goal": {"dod_tier": "space_station"}})
        self.assertIsNone(ct.load_goal_manifest(self.root, on_warn=_quiet)["goal"])

    def test_quiescence_bool_rejected(self):
        self._manifest({"quiescence_cycles": True})
        m = ct.load_goal_manifest(self.root, on_warn=_quiet)
        self.assertIsNone(m["quiescence_cycles"])

    def test_quiescence_positive_int_kept(self):
        self._manifest({"quiescence_cycles": 3})
        self.assertEqual(
            ct.load_goal_manifest(self.root, on_warn=_quiet)["quiescence_cycles"], 3)


# --------------------------------------------------------------------------- #
# Goal predicate — real store for the gap leg, patched leaf for eval
# --------------------------------------------------------------------------- #
class TestGoalPredicate(_Base):
    def test_empty_goal_never_fires(self):
        v = ct.goal_predicate(self._app(), {"goal": {}}, on_warn=_quiet)
        self.assertFalse(v["met"])

    def test_all_three_met_terminates(self):
        app = self._app()
        self._gap_report(app)              # scan ran, and...
        _Store(app)                        # ...store exists, no gaps published
        self._adherence(app, "PASS")
        self._patch_eval(compile_ran=True, done=True, composite=80)
        v = ct.goal_predicate(app, {"goal": {"doc_gap_empty": True,
                              "dod_tier": "v1", "eval_threshold": 70}},
                              on_warn=_quiet)
        self.assertTrue(v["met"])
        self.assertEqual(set(v["checks"]),
                         {"doc_gap_empty", "dod_tier", "eval_threshold"})

    def test_single_check_goal_fires_on_its_own(self):
        app = self._app()
        self._patch_eval(compile_ran=True, composite=90)
        v = ct.goal_predicate(app, {"goal": {"eval_threshold": 70}},
                              on_warn=_quiet)
        self.assertTrue(v["met"])

    def test_open_gap_blocks_and_is_recorded(self):
        app = self._app()
        self._gap_report(app)
        _Store(app).gap("Missing tests")   # a real, final gap artifact
        v = ct.goal_predicate(app, {"goal": {"doc_gap_empty": True}},
                              on_warn=_quiet)
        self.assertFalse(v["met"])
        self.assertFalse(v["checks"]["doc_gap_empty"])
        self.assertEqual(v["evidence"]["doc_gap_empty"]["open_gap_count"], 1)

    def test_gap_check_requires_the_scan_report(self):
        # POSITIVE-evidence guard: a never-rendered session lists zero gaps but
        # must NOT pass — no GAP_REPORT.md means no scan ran.
        app = self._app()
        _Store(app)                        # store exists, zero gaps, NO report
        v = ct.goal_predicate(app, {"goal": {"doc_gap_empty": True}},
                              on_warn=_quiet)
        self.assertFalse(v["met"])
        self.assertEqual(v["evidence"]["doc_gap_empty"]["error"], "no_gap_report")

    def test_open_gaps_query_only_counts_final(self):
        # A draft (non-final) gap must NOT count as open — pins the status
        # filter (a widened filter would over-report and never terminate).
        app = self._app()
        st = _Store(app)
        st.gap("final gap", consensus=True)        # -> final
        st.gap("draft gap", consensus=False)       # -> pending_review
        self.assertEqual(len(ct._open_gaps(app, _quiet)), 1)

    def test_eval_untouched_project_scoring_zero_fails(self):
        # composite 0 from a never-built project must not satisfy threshold 0.
        app = self._app()
        self._patch_eval(compile_ran=False, done=False, composite=0)
        v = ct.goal_predicate(app, {"goal": {"eval_threshold": 0}},
                              on_warn=_quiet)
        self.assertFalse(v["met"])
        self.assertEqual(v["evidence"]["eval_threshold"]["error"], "not_evaluated")

    def test_eval_below_threshold_fails(self):
        app = self._app()
        self._patch_eval(compile_ran=True, composite=50)
        v = ct.goal_predicate(app, {"goal": {"eval_threshold": 70}},
                              on_warn=_quiet)
        self.assertFalse(v["met"])

    def test_missing_adherence_fails_dod(self):
        v = ct.goal_predicate(self._app(), {"goal": {"dod_tier": "v1"}},
                              on_warn=_quiet)
        self.assertFalse(v["met"])
        self.assertEqual(v["evidence"]["dod_tier"]["error"], "no_adherence_grade")

    def test_eval_crash_fails_safe(self):
        app = self._app()
        import evalharness
        self._orig_eval = evalharness.score_project
        self.addCleanup(self._restore_eval)
        def boom(*_a, **_k):
            raise RuntimeError("harness down")
        evalharness.score_project = boom
        v = ct.goal_predicate(app, {"goal": {"eval_threshold": 70}},
                              on_warn=_quiet)
        self.assertFalse(v["met"])


# --------------------------------------------------------------------------- #
# Quiescence — real store; the oscillation trap uses a real supersedes chain
# --------------------------------------------------------------------------- #
class TestQuiescence(_Base):
    def test_new_final_resets_counter(self):
        app = self._app()
        st = _Store(app)
        st.idea("A", "one\n")
        rec, conv = ct.quiescence_step(None, app, 2, "d0")       # baseline
        self.assertEqual(rec["idle"], 0)
        self.assertFalse(conv)
        st.idea("B", "two\n")                                     # a new final
        rec, conv = ct.quiescence_step(rec, app, 2, "d0")
        self.assertEqual(rec["idle"], 0)

    def test_idle_counts_up_to_limit(self):
        app = self._app()
        _Store(app).idea("A", "one\n")
        rec, _ = ct.quiescence_step(None, app, 2, "d0")           # idle 0
        rec, conv = ct.quiescence_step(rec, app, 2, "d0")          # idle 1
        self.assertEqual((rec["idle"], conv), (1, False))
        rec, conv = ct.quiescence_step(rec, app, 2, "d0")          # idle 2
        self.assertTrue(conv)

    def test_oscillation_is_not_progress(self):
        # A(one) -> B supersedes A (two) -> A' supersedes B (one again). A'
        # re-derives an ancestor's content, so it is NOT a genuine new final
        # and must not reset the idle counter (the card's oscillation trap,
        # mapped onto the real store's content-convergence semantics).
        app = self._app()
        st = _Store(app)
        a = st.idea("A", "one\n")
        b = st.idea("A", "two\n", supersedes=a)
        rec, _ = ct.quiescence_step(None, app, 2, "d0")           # baseline
        rec, _ = ct.quiescence_step(rec, app, 2, "d0")             # idle 1
        st.idea("A", "one\n", supersedes=b)                        # oscillation
        rec, conv = ct.quiescence_step(rec, app, 2, "d0")          # idle 2, not 0
        self.assertEqual(rec["idle"], 2)
        self.assertTrue(conv)

    def test_genuine_finals_query_excludes_non_final(self):
        # a draft artifact must not count as a live final (pins status filter).
        app = self._app()
        st = _Store(app)
        st.idea("A", "one\n", consensus=True)      # final
        st.idea("B", "two\n", consensus=False)     # pending_review
        self.assertEqual(len(ct.genuine_final_ids(app, _quiet)), 1)

    def test_progress_digest_change_resets(self):
        app = self._app()
        _Store(app).idea("A", "one\n")
        rec, _ = ct.quiescence_step(None, app, 2, "d0")           # idle 0
        rec, _ = ct.quiescence_step(rec, app, 2, "d0")             # idle 1
        rec, conv = ct.quiescence_step(rec, app, 2, "d1")          # phase moved
        self.assertEqual((rec["idle"], conv), (0, False))

    def test_progress_digest_excludes_last_processed(self):
        # the heartbeat field must NOT change the digest (else stuck-but-alive
        # sessions never go quiescent).
        a = ct.progress_digest({"current_phase": "x", "last_processed": "t1"})
        b = ct.progress_digest({"current_phase": "x", "last_processed": "t2"})
        self.assertEqual(a, b)
        c = ct.progress_digest({"current_phase": "y", "last_processed": "t1"})
        self.assertNotEqual(a, c)


# --------------------------------------------------------------------------- #
# Wiring into full_poll + reconcile crash-safety + bounded-time
# --------------------------------------------------------------------------- #
class TestTerminationWiring(_Base):
    def _session(self, sid, sstate):
        app_dir = self._app(sid)
        project = sid.split("/")[0]
        open(os.path.join(self.root, project, ".orch-sections"), "w").close()
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("x")
        with open(os.path.join(app_dir, "agent_state.json"), "w") as fh:
            json.dump(sstate, fh)
        return app_dir

    def test_no_manifest_no_termination_side_effects(self):
        self._session("p/documentation/c1", {"current_phase": "brief"})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertEqual(st["terminated"], {})
        self.assertFalse(os.path.exists(
            os.path.join(self.root, ".conductor", "reports")))

    def test_terminal_event_is_durable_before_state_save_crash(self):
        app = self._session("p/documentation/c1", {"current_phase": "x"})
        state = cond.default_state()
        evidence = {"reason": "vote_undecided",
                    "evidence": {"undecided": 2, "limit": 2}}
        with unittest.mock.patch.object(
                cond, "save_conductor_state",
                side_effect=RuntimeError("crash after ledger+event")):
            with self.assertRaisesRegex(RuntimeError, "crash after"):
                cond._record_termination(
                    self.root, state, "p/documentation/c1", "stalled",
                    evidence, emit=_quiet)
        notices = evlib.read_events(app, kinds=("stalled",))
        self.assertEqual(1, len(notices))
        self.assertEqual("vote_undecided", notices[0]["reason"])

    def test_goal_met_terminates_with_report_and_ledger(self):
        app = self._session("p/documentation/c1",
                            {"current_phase": "done", "done": True})
        self._gap_report(app)
        _Store(app)
        self._adherence(app, "PASS")
        self._patch_eval(compile_ran=True, done=True, composite=90)
        self._manifest({"goal": {"doc_gap_empty": True, "dod_tier": "v1",
                                 "eval_threshold": 0}})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertEqual(st["terminated"]["p/documentation/c1"]["reason"],
                         "goal_met")
        led = cond.read_ledger(self.root)
        self.assertTrue(any(r.get("decision") == "goal_met" for r in led))
        # the goal_met report file exists and records each sub-check
        rpath = st["terminated"]["p/documentation/c1"]["report"]
        with open(rpath, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertTrue(report["payload"]["met"])
        self.assertEqual(set(report["payload"]["checks"]),
                         {"doc_gap_empty", "dod_tier", "eval_threshold"})

    def test_self_reported_done_does_not_terminate(self):
        # §23: a session CLAIMING done (agent_state) with the goal NOT actually
        # met (missing adherence grade) must NOT be terminated.
        self._session("p/documentation/c1",
                      {"current_phase": "done", "done": True})
        self._manifest({"goal": {"dod_tier": "v1"}})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertEqual(st["terminated"], {})

    def test_never_run_session_not_terminated(self):
        # discovered (has initial_prompt) but no agent_state.json -> not started
        app_dir = self._app("p/documentation/c1")
        open(os.path.join(self.root, "p", ".orch-sections"), "w").close()
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("x")
        self._manifest({"goal": {"doc_gap_empty": True}, "quiescence_cycles": 1})
        st = cond.default_state()
        for _ in range(4):
            st = cond.full_poll(self.root, st, emit=_quiet)
        self.assertEqual(st["terminated"], {})

    def test_corrupt_manifest_safe_default_via_full_poll(self):
        self._session("p/documentation/c1", {"current_phase": "x"})
        with open(os.path.join(self.root, "goal_manifest.json"), "w") as fh:
            fh.write("{broken")
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertEqual(st["terminated"], {})

    def test_unreachable_goal_ends_via_quiescence(self):
        app = self._session("p/documentation/c1", {"current_phase": "stuck"})
        self._gap_report(app)
        _Store(app).gap("permanent gap")   # goal (doc_gap_empty) never met
        self._manifest({"goal": {"doc_gap_empty": True}, "quiescence_cycles": 2})
        st = cond.default_state()
        for _ in range(6):
            st = cond.full_poll(self.root, st, emit=_quiet)
            if "p/documentation/c1" in st["terminated"]:
                break
        self.assertEqual(st["terminated"]["p/documentation/c1"]["reason"],
                         "converged_open_items")
        rpath = st["terminated"]["p/documentation/c1"]["report"]
        with open(rpath, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertTrue(report["payload"]["open_gaps"])
        self.assertTrue(report["payload"]["unmet_goal_checks"])
        notices = evlib.read_events(app, kinds=("converged",))
        self.assertEqual(1, len(notices))
        self.assertEqual("documentation", notices[0]["section"])
        self.assertIn("idle cycles", notices[0]["reason"])

    def test_quiescence_termination_takes_snapshot_after_terminal_ledger(self):
        app = self._session("p/documentation/c1", {"current_phase": "stuck"})
        self._gap_report(app)
        _Store(app).gap("permanent gap")
        self._manifest({"goal": {"doc_gap_empty": True},
                        "quiescence_cycles": 1})
        state = cond.default_state()
        calls = []
        with unittest.mock.patch.object(
                cond, "snapshot",
                side_effect=lambda root, reason, cursor, emit: calls.append(
                    (reason, cursor, cond.read_ledger(root)[-1]["decision"]))):
            for _ in range(4):
                state = cond.full_poll(self.root, state, emit=_quiet)
                if calls:
                    break
        self.assertEqual("converged_open_items", calls[0][2])
        self.assertTrue(calls[0][0].startswith("quiescence:"))
        self.assertEqual(state["ledger_cursor"], calls[0][1])

    def test_terminated_session_skipped_by_routing(self):
        self._session("p/documentation/c1", {"current_phase": "done"})
        state = cond.default_state()
        state["terminated"]["p/documentation/c1"] = {"reason": "goal_met"}
        minted = []
        import sessions as seslib_local
        orig = seslib_local.mint_delegation_session
        seslib_local.mint_delegation_session = \
            lambda *a, **k: minted.append(a) or None
        try:
            cond.route_engine(self.root, state,
                              ["p/documentation/c1"], emit=_quiet)
        finally:
            seslib_local.mint_delegation_session = orig
        self.assertEqual(minted, [])


class TestReconcileRebuildsTerminated(_Base):
    """Crash between a termination's ledger append and its state save: the
    ledger line is durable but state['terminated'] was never persisted.
    reconcile_on_start must rebuild it so the session is not re-terminated."""

    def test_uncursored_termination_line_is_replayed(self):
        # append a termination decision straight to the ledger (simulating the
        # durable-append half of a crashed _record_termination)...
        cond.ledger_append(self.root, {
            "v": 1, "ts": 123.0, "stage": "evaluating",
            "decision": "goal_met", "session": "p/documentation/c1",
            "detail": {"reason": "goal_met", "report": "/tmp/r.json"}})
        # ...with a state whose cursor still sits before it (the save never ran)
        state = cond.default_state()   # cursor 0, terminated {}
        state = cond.reconcile_on_start(self.root, state, emit=_quiet)
        self.assertIn("p/documentation/c1", state["terminated"])
        self.assertEqual(state["terminated"]["p/documentation/c1"]["reason"],
                         "goal_met")


# --------------------------------------------------------------------------- #
# 7.5b: budgets (pure)
# --------------------------------------------------------------------------- #
class TestBudgets(_Base):
    def _led(self, *decisions, ts0=100.0):
        return [{"decision": d, "ts": ts0 + i}
                for i, d in enumerate(decisions)]

    def _rec(self, provider, cost_micro, ts):
        return {"provider": provider, "cost_micro_usd": cost_micro, "ts": ts}

    def test_count_turns_counts_only_mints(self):
        led = self._led("observed", "route_approved", "route_denied",
                        "route_approved")
        self.assertEqual(ct.count_turns(led), 2)

    def test_count_turns_includes_restart_recovered_mints(self):
        # a route_recovered line means a mint ALREADY happened before a crash
        # (7.3 restart recovery) — it must count as a real turn, or a host
        # that crashes right after each mint could take unbounded turns while
        # the cap reports 0 used.
        led = self._led("route_approved", "route_recovered", "route_recovered")
        self.assertEqual(ct.count_turns(led), 3)

    def test_wall_clock_from_first_entry(self):
        led = [{"decision": "observed", "ts": 100.0},
               {"decision": "observed", "ts": 150.0}]
        self.assertEqual(ct.wall_clock_elapsed(led, 250.0), 150.0)
        self.assertEqual(ct.wall_clock_elapsed([], 250.0), 0.0)

    def test_provider_spend_usd(self):
        recs = [self._rec("google", 500_000, "t"),
                self._rec("google", 500_000, "t"),
                self._rec("anthropic", 2_000_000, "t")]
        spend = ct.provider_spend_usd(recs)
        self.assertEqual(spend["google"], 1.0)
        self.assertEqual(spend["anthropic"], 2.0)

    def test_provider_requests_today_filters_by_date(self):
        recs = [self._rec("google", 0, "2026-07-20 09:00:00"),
                self._rec("google", 0, "2026-07-20 10:00:00"),
                self._rec("google", 0, "2026-07-19 23:59:00")]
        self.assertEqual(
            ct.provider_requests_today(recs, "2026-07-20")["google"], 2)

    def test_turns_cap_exhausts(self):
        bc = ct.budget_check({"turns": 2},
                             self._led("route_approved", "route_approved"),
                             [], 200.0, "2026-07-20")
        self.assertTrue(bc["exhausted"])
        self.assertEqual(bc["reason"], "turns_exhausted")

    def test_wall_clock_cap_exhausts(self):
        led = [{"decision": "observed", "ts": 100.0}]
        bc = ct.budget_check({"wall_clock_s": 50}, led, [], 200.0, "2026-07-20")
        self.assertTrue(bc["exhausted"])
        self.assertEqual(bc["reason"], "wall_clock_exhausted")

    def test_spend_cap_exhausts(self):
        recs = [self._rec("google", 1_500_000, "t")]
        bc = ct.budget_check({"per_provider": {"google": {"spend": 1.0}}},
                             [], recs, 0.0, "2026-07-20")
        self.assertTrue(bc["exhausted"])
        self.assertEqual((bc["reason"], bc["provider"]),
                         ("spend_exhausted", "google"))

    def test_daily_quota_is_soft_over_quota_not_exhausted(self):
        recs = [self._rec("google", 0, "2026-07-20 09:00:00"),
                self._rec("google", 0, "2026-07-20 10:00:00")]
        bc = ct.budget_check(
            {"per_provider": {"google": {"requests_per_day": 2}}},
            [], recs, 0.0, "2026-07-20")
        self.assertFalse(bc["exhausted"])
        self.assertIn("google", bc["over_quota"])

    def test_missing_caps_not_enforced(self):
        bc = ct.budget_check({"turns": None, "per_provider": "nope"},
                             self._led("route_approved"), [], 0.0, "2026-07-20")
        self.assertFalse(bc["exhausted"])
        self.assertEqual(bc["over_quota"], set())


# --------------------------------------------------------------------------- #
# 7.5b: stall (real store for oscillation)
# --------------------------------------------------------------------------- #
class TestStall(_Base):
    def test_vote_undecided_twice_stalls(self):
        s = ct.stall_check(self._app(),
                           {"phase_resolutions": {"p1": "vote_undecided",
                                                  "p2": "vote_undecided",
                                                  "p3": "idle_timeout"}},
                           {"vote_undecided_limit": 2})
        self.assertTrue(s["stalled"])
        self.assertEqual(s["reason"], "vote_undecided")

    def test_vote_undecided_once_not_stalled(self):
        s = ct.stall_check(self._app(),
                           {"phase_resolutions": {"p1": "vote_undecided"}}, {})
        self.assertFalse(s["stalled"])

    def test_default_limit_is_two(self):
        s = ct.stall_check(self._app(),
                           {"phase_resolutions": {"a": "vote_undecided",
                                                  "b": "vote_undecided"}}, None)
        self.assertTrue(s["stalled"])   # cfg None -> default limit 2

    def test_oscillation_stalls(self):
        app = self._app()
        st = _Store(app)
        a = st.idea("A", "one\n")
        b = st.idea("A", "two\n", supersedes=a)
        st.idea("A", "one\n", supersedes=b)   # re-derives ancestor A's content
        s = ct.stall_check(app, {"phase_resolutions": {}}, {})
        self.assertTrue(s["stalled"])
        self.assertEqual(s["reason"], "supersedes_oscillation")


# --------------------------------------------------------------------------- #
# 7.5b: budget + stall wiring through full_poll / route_engine
# --------------------------------------------------------------------------- #
class TestBudgetStallWiring(_Base):
    def _session(self, sid, sstate):
        app_dir = self._app(sid)
        project = sid.split("/")[0]
        open(os.path.join(self.root, project, ".orch-sections"), "w").close()
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("x")
        with open(os.path.join(app_dir, "agent_state.json"), "w") as fh:
            json.dump(sstate, fh)
        return app_dir

    def test_turns_budget_halts_workspace(self):
        self._session("p/documentation/c1", {"current_phase": "x"})
        cond.ledger_append(self.root, {"decision": "route_approved", "ts": 1.0})
        cond.ledger_append(self.root, {"decision": "route_approved", "ts": 2.0})
        self._manifest({"budgets": {"turns": 2}})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertIsNotNone(st["halted"])
        self.assertEqual(st["halted"]["reason"], "turns_exhausted")
        led = cond.read_ledger(self.root)
        self.assertTrue(any(r.get("decision") == "budget_exhausted"
                            for r in led))
        notices = evlib.read_events(os.path.join(self.root, ".conductor"),
                                    kinds=("budget_exhausted",))
        self.assertEqual(1, len(notices))
        self.assertEqual("turns_exhausted", notices[0]["budget"])
        self.assertEqual("Workspace", notices[0]["project"])
        self.assertIn("2 used / 2 cap", notices[0]["measurement"])

    def test_halted_state_stops_routing(self):
        # a genuinely routable scenario: without the halt this WOULD mint.
        import conductor_routing as crlib
        import artifacts as artlib
        os.makedirs(os.path.join(self.root, "proj/ideas/chat-1"), exist_ok=True)
        state = cond.default_state()
        state["halted"] = {"reason": "turns_exhausted"}
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                "lineage": [], "hop_count": 0, "status": "final"}
        minted = []
        with unittest.mock.patch.object(
                crlib, "load_route_config",
                lambda *a, **k: crlib.RouteConfig(routes={"idea": "research"})), \
                unittest.mock.patch.object(artlib, "list_artifacts",
                                           lambda *a, **k: [meta]), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(artlib, "lineage_index",
                                           lambda *a, **k: {}), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    lambda *a, **k: minted.append(a) or "x"):
            cond.route_engine(self.root, state, ["proj/ideas/chat-1"],
                              emit=_quiet)
        self.assertEqual(minted, [])                 # halt short-circuits routing
        self.assertEqual(cond.read_ledger(self.root), [])   # nothing even planned

    def test_stall_terminates_session(self):
        self._session("p/documentation/c1",
                      {"current_phase": "x",
                       "phase_resolutions": {"a": "vote_undecided",
                                             "b": "vote_undecided"}})
        self._manifest({"stall": {"vote_undecided_limit": 2}})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertEqual(st["terminated"]["p/documentation/c1"]["reason"],
                         "stalled")
        notices = evlib.read_events(
            os.path.join(self.root, "p/documentation/c1"), kinds=("stalled",))
        self.assertEqual(1, len(notices))
        self.assertEqual("vote_undecided", notices[0]["reason"])
        self.assertEqual("documentation", notices[0]["section"])

    def test_over_quota_route_is_deferred_not_minted(self):
        import conductor_routing as crlib
        import artifacts as artlib
        # target section "research" has been running on google (over quota)
        research = self._app("proj/research/chat-1")
        with open(os.path.join(research, "costs.jsonl"), "w") as fh:
            fh.write(json.dumps({"provider": "google",
                                 "ts": "2026-07-20 09:00:00"}) + "\n")
        sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(self.root, sid), exist_ok=True)
        state = cond.default_state()
        state["over_quota"] = ["google"]
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                "lineage": [], "hop_count": 0, "status": "final"}
        minted = []

        def cfg_for(_sd, section, *_a, **_k):
            return crlib.RouteConfig(routes={"idea": "research"}) \
                if section == "ideas" else crlib.RouteConfig()

        def arts(app_dir, *a, **k):
            return [meta] if app_dir.endswith("ideas/chat-1") else []

        with unittest.mock.patch.object(crlib, "load_route_config", cfg_for), \
                unittest.mock.patch.object(artlib, "list_artifacts", arts), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(artlib, "lineage_index",
                                           lambda *a, **k: {}), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    lambda *a, **k: minted.append(a) or "x"):
            cond.route_engine(self.root, state,
                              ["proj/ideas/chat-1", "proj/research/chat-1"],
                              emit=_quiet)
        self.assertEqual(minted, [])   # deferred, never minted
        led = cond.read_ledger(self.root)
        deferred = [r for r in led if r and r.get("decision") == "route_deferred"]
        self.assertTrue(deferred)
        self.assertEqual(deferred[0]["detail"]["provider"], "google")

    def test_goal_met_not_swallowed_by_same_poll_halt(self):
        # order is goal -> stall -> budget -> quiescence: a session that
        # genuinely finished its goal THIS poll must get its goal_met record
        # even though the same poll's global budget also exhausts.
        app = self._session("p/documentation/c1",
                            {"current_phase": "done", "done": True})
        self._gap_report(app)
        _Store(app)
        self._adherence(app, "PASS")
        self._patch_eval(compile_ran=True, done=True, composite=90)
        cond.ledger_append(self.root, {"decision": "route_approved", "ts": 1.0})
        cond.ledger_append(self.root, {"decision": "route_approved", "ts": 2.0})
        self._manifest({"goal": {"doc_gap_empty": True, "dod_tier": "v1",
                                 "eval_threshold": 0},
                        "budgets": {"turns": 2}})
        st = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertEqual(st["terminated"]["p/documentation/c1"]["reason"],
                         "goal_met")
        self.assertIsNotNone(st["halted"])   # both happened, neither swallowed

    def test_corrupt_manifest_keeps_last_known_budget_enforced(self):
        # a manifest that WAS valid (budgets active) and becomes corrupt on a
        # later poll (a torn write) must not silently go uncapped.
        self._session("p/documentation/c1", {"current_phase": "x"})
        self._manifest({"budgets": {"turns": 2}})
        state = cond.full_poll(self.root, cond.default_state(), emit=_quiet)
        self.assertIsNone(state["halted"])   # not exhausted yet
        with open(os.path.join(self.root, "goal_manifest.json"), "w") as fh:
            fh.write("{broken")
        cond.ledger_append(self.root, {"decision": "route_approved", "ts": 1.0})
        cond.ledger_append(self.root, {"decision": "route_approved", "ts": 2.0})
        state = cond.full_poll(self.root, state, emit=_quiet)
        self.assertIsNotNone(state["halted"])   # still enforced, not dropped
        self.assertEqual(state["halted"]["reason"], "turns_exhausted")

    def test_quota_deferral_retries_after_quota_resets(self):
        import conductor_routing as crlib
        import artifacts as artlib
        research = self._app("proj/research/chat-1")
        with open(os.path.join(research, "costs.jsonl"), "w") as fh:
            fh.write(json.dumps({"provider": "google",
                                 "ts": "2026-07-20 09:00:00"}) + "\n")
        os.makedirs(os.path.join(self.root, "proj/ideas/chat-1"), exist_ok=True)
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
               "lineage": [], "hop_count": 0, "status": "final"}
        minted = []

        def cfg_for(_sd, section, *_a, **_k):
            return crlib.RouteConfig(routes={"idea": "research"}) \
                if section == "ideas" else crlib.RouteConfig()

        def arts(app_dir, *a, **k):
            return [meta] if app_dir.endswith("ideas/chat-1") else []

        state = cond.default_state()   # ONE state, carried across both polls —
        # a deferred route must not be marked routed, or it would never retry.

        def run(over_quota):
            state["over_quota"] = over_quota
            with unittest.mock.patch.object(crlib, "load_route_config",
                                            cfg_for), \
                    unittest.mock.patch.object(artlib, "list_artifacts", arts), \
                    unittest.mock.patch.object(artlib, "is_admissible",
                                               lambda *a, **k: True), \
                    unittest.mock.patch.object(artlib, "lineage_index",
                                               lambda *a, **k: {}), \
                    unittest.mock.patch("sessions.mint_delegation_session",
                                        lambda *a, **k: minted.append(a) or "x"):
                return cond.route_engine(
                    self.root, state,
                    ["proj/ideas/chat-1", "proj/research/chat-1"],
                    emit=_quiet)

        run(["google"])
        self.assertEqual(minted, [])   # deferred: quota still over
        run([])                        # quota reset (a new day / freed budget)
        self.assertEqual(len(minted), 1)   # the SAME route now fires

    def test_drain_pending_defers_approved_route_over_quota(self):
        import conductor_permissions as cplib
        research = self._app("proj/research/chat-1")
        with open(os.path.join(research, "costs.jsonl"), "w") as fh:
            fh.write(json.dumps({"provider": "google",
                                 "ts": "2026-07-20 09:00:00"}) + "\n")
        action = {"action_id": "r1", "target": "research",
                 "payload": {"artifact_id": "a1", "content_hash": "h1",
                            "source_section": "ideas", "rule_id": "route:idea"},
                 "requested_by": "proj/ideas/chat-1"}
        cplib.enqueue_pending(self.root, action)
        adir = cplib.approvals_dir(self.root)
        os.makedirs(adir, exist_ok=True)
        open(os.path.join(adir, "r1.ok"), "w").close()
        state = cond.default_state()
        state["over_quota"] = ["google"]
        minted = []
        import sessions as seslib_local
        orig = seslib_local.mint_delegation_session
        seslib_local.mint_delegation_session = \
            lambda *a, **k: minted.append(a) or "x"
        try:
            cond.route_engine(self.root, state, ["proj/research/chat-1"],
                              emit=_quiet)
        finally:
            seslib_local.mint_delegation_session = orig
        self.assertEqual(minted, [])           # deferred, not executed
        self.assertTrue(cplib.is_pending(self.root, "r1"))  # still queued

    def test_budget_halt_survives_restart_via_reconcile(self):
        cond.ledger_append(self.root, {
            "decision": "budget_exhausted", "session": None, "ts": 5.0,
            "detail": {"reason": "spend_exhausted", "report": "/tmp/r.json"}})
        state = cond.reconcile_on_start(self.root, cond.default_state(),
                                        emit=_quiet)
        self.assertIsNotNone(state["halted"])
        self.assertEqual(state["halted"]["reason"], "spend_exhausted")


if __name__ == "__main__":
    unittest.main()
