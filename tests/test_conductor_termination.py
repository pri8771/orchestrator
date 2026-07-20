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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import artifacts as artlib
import conductor as cond
import conductor_termination as ct


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


if __name__ == "__main__":
    unittest.main()
