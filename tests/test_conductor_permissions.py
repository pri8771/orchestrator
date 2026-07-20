"""V3 7.4b: conductor route permissions — the capability gate, non-blocking
approval queue, and pending-action lifecycle (enqueue/decide/execute/remove),
restart-safe.
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import conductor as cond
import conductor_permissions as cp
import conductor_routing as cr


class _Intent:
    def __init__(self, rid="rid1", target="research"):
        self._i = cr.RouteIntent("a1", "h1", "ideas", target, "one",
                                 "r1", cr.ALLOW)
        self._rid = rid

    @property
    def route_id(self):
        return self._rid

    def __getattr__(self, k):
        return getattr(self._i, k)


class TestPendingQueue(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_enqueue_read_idempotent_remove(self):
        act = cp.pending_action(_Intent("rid-a"), "proj/ideas/chat-1")
        self.assertTrue(cp.enqueue_pending(self.root, act))
        self.assertTrue(cp.enqueue_pending(self.root, act))   # idempotent
        self.assertEqual(len(cp.read_pending(self.root)), 1)
        self.assertTrue(cp.is_pending(self.root, "rid-a"))
        cp.remove_pending(self.root, "rid-a")
        self.assertEqual(cp.read_pending(self.root), [])

    def test_malformed_line_skipped(self):
        cp.enqueue_pending(self.root, cp.pending_action(_Intent("rid-a"), "s"))
        with open(cp.pending_path(self.root), "a", encoding="utf-8") as fh:
            fh.write("not json\n")
        self.assertEqual(len(cp.read_pending(self.root)), 1)

    def test_pending_survives_reload(self):
        cp.enqueue_pending(self.root, cp.pending_action(_Intent("rid-x"), "s"))
        # a fresh process reads the same queue off disk
        self.assertTrue(cp.is_pending(self.root, "rid-x"))


class TestApprovalDecision(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(cp.approvals_dir(self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, name):
        open(os.path.join(cp.approvals_dir(self.root), name), "w").close()

    def test_pending_ok_changes_and_deny_wins(self):
        self.assertIsNone(cp.approval_decision(self.root, "r1"))
        self._touch("r1.ok")
        self.assertEqual(cp.approval_decision(self.root, "r1"), "approved")
        self._touch("r1.changes")   # both present -> deny wins
        self.assertEqual(cp.approval_decision(self.root, "r1"), "rejected")


class _RouteBase(unittest.TestCase):
    """Drives the real route_engine with an escalated target section."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(self.root, self.sid))
        import artifacts as artlib
        import sections as seclib
        self.artlib, self.seclib = artlib, seclib
        self.meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                     "lineage": [], "hop_count": 0, "status": "final"}
        self.cfg = cr.RouteConfig(routes={"idea": "research"})
        # 'research' escalates beyond workspace-only -> route needs approval.
        self._escalated = seclib.Section(
            id="research", title="R", workflow=None, workflow_name="(inline)",
            default_mode="chat", artifact_types_emitted=[],
            artifact_types_accepted=[], dod_tier="standard",
            capabilities={"writes": "workspace", "exec": False,
                          "external": True})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, state, mint):
        with unittest.mock.patch.object(cr, "load_route_config",
                                        lambda *a, **k: self.cfg), \
                unittest.mock.patch.object(self.artlib, "list_artifacts",
                                           lambda *a, **k: [self.meta]), \
                unittest.mock.patch.object(self.artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(
                    self.artlib, "lineage_index",
                    lambda *a, **k: {"by_id": {"a1": self.meta}}), \
                unittest.mock.patch.object(self.seclib, "load_section",
                                           lambda *a, **k: self._escalated), \
                unittest.mock.patch("sessions.mint_delegation_session", mint), \
                unittest.mock.patch("sessions.scan_effected_routes",
                                    lambda *a: set()):
            cond.route_engine(self.root, state, [self.sid], emit=lambda *a: None)


class TestCapabilityGate(_RouteBase):
    def test_escalated_route_queues_instead_of_minting(self):
        def mint(*a, **k):
            raise AssertionError("escalated route must NOT mint directly")
        state = cond.default_state()
        self._run(state, mint)
        pend = cp.read_pending(self.root)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["target"], "research")
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertTrue(any(r["decision"] == "approval_requested"
                            for r in recs))
        self.assertNotIn(pend[0]["route_id"], state["routed"])

    def test_approval_ok_executes_the_gated_route(self):
        state = cond.default_state()
        self._run(state, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no mint before approval")))
        rid = cp.read_pending(self.root)[0]["route_id"]
        # operator approves
        os.makedirs(cp.approvals_dir(self.root), exist_ok=True)
        open(os.path.join(cp.approvals_dir(self.root), "%s.ok" % rid),
             "w").close()
        minted = []
        self._run(state, lambda *a, **k: minted.append(1) or "/d")
        self.assertEqual(minted, [1])   # now it mints
        self.assertEqual(cp.read_pending(self.root), [])   # dequeued
        self.assertIn(rid, state["routed"])
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertTrue(any(r["decision"] == "route_approved" for r in recs))

    def test_changes_rejects_without_minting(self):
        state = cond.default_state()
        self._run(state, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no mint before decision")))
        rid = cp.read_pending(self.root)[0]["route_id"]
        os.makedirs(cp.approvals_dir(self.root), exist_ok=True)
        open(os.path.join(cp.approvals_dir(self.root), "%s.changes" % rid),
             "w").close()
        def mint(*a, **k):
            raise AssertionError("a rejected route must never mint")
        self._run(state, mint)
        self.assertEqual(cp.read_pending(self.root), [])   # resolved
        self.assertIn(rid, state["routed"])   # won't re-enqueue
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertTrue(any(r["decision"] == "route_denied" for r in recs))

    def test_re_poll_while_pending_does_not_duplicate(self):
        state = cond.default_state()
        self._run(state, lambda *a, **k: None)
        self._run(state, lambda *a, **k: None)   # second poll, still pending
        self.assertEqual(len(cp.read_pending(self.root)), 1)
        reqs = [r for r in cond.read_ledger(self.root)
                if r and r["decision"] == "approval_requested"]
        self.assertEqual(len(reqs), 1, "approval requested once, not per poll")


class TestApprovedRouteCrashSafety(_RouteBase):
    """A crash between _drain_pending's ledger+state save and its
    remove_pending call (real SIGKILL-style: no exception handler runs) must
    not re-ledger a duplicate decision or re-mint on restart. Simulated by
    letting the ledger+state save happen for real, then preventing
    remove_pending from running (as a real crash would), reloading state from
    disk (what a fresh process actually sees), and re-draining."""

    def _approve(self):
        state = cond.default_state()
        self._run(state, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("no mint before approval")))
        rid = cp.read_pending(self.root)[0]["route_id"]
        os.makedirs(cp.approvals_dir(self.root), exist_ok=True)
        open(os.path.join(cp.approvals_dir(self.root), "%s.ok" % rid),
             "w").close()
        return rid

    def test_crash_before_remove_pending_does_not_double_ledger_or_remint(self):
        rid = self._approve()
        minted = []

        def mint(*a, **k):
            minted.append(1)
            return "/some/dir"

        # First drain: mint + ledger + state save happen for real, THEN
        # remove_pending raises — standing in for a hard crash (SIGKILL)
        # that kills the process at exactly that line. A no-op stub here
        # would NOT reproduce the bug: execution would still reach whatever
        # save happens after remove_pending in the buggy version, silently
        # making the crash window untestable. Raising is what actually stops
        # execution at that point, the way a real crash would.
        with unittest.mock.patch.object(
                cp, "remove_pending",
                lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("simulated crash"))):
            state = cond.default_state()
            with self.assertRaises(RuntimeError):
                self._run(state, mint)
        self.assertEqual(minted, [1])
        self.assertEqual(len(cp.read_pending(self.root)), 1,
                         "pending file is stale, as a real crash would leave it")

        # "Restart": a fresh process loads state from what was actually
        # persisted to disk — NOT the in-memory `state` dict from above.
        reloaded = cond.load_conductor_state(self.root)
        self.assertIn(rid, reloaded["routed"],
                     "routed must have been persisted in the SAME save as "
                     "the ledger line, or this assertion is exactly what "
                     "the bug violates")

        # Re-drain with the real remove_pending restored.
        self._run(reloaded, mint)

        self.assertEqual(minted, [1], "must NOT mint a second time")
        self.assertEqual(cp.read_pending(self.root), [],
                         "the stale pending record is cleaned up on restart")
        approved = [r for r in cond.read_ledger(self.root)
                   if r and r.get("decision") == "route_approved"]
        self.assertEqual(len(approved), 1,
                         "must NOT double-ledger route_approved for the "
                         "same rid across the crash+restart")


if __name__ == "__main__":
    unittest.main()


class TestReviewFixes(_RouteBase):
    def test_pending_route_never_reclassified_when_caps_flip(self):
        # CRITICAL bypass: a route gated (queued, unapproved) on poll 1 must
        # NOT mint on poll 2 even if the target section momentarily reads as
        # workspace-only (torn manifest read / GUI edit lowering caps). Once
        # pending, only an approval file may release it.
        state = cond.default_state()
        self._run(state, lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("poll 1 must gate, not mint")))
        rid = cp.read_pending(self.root)[0]["route_id"]

        # Poll 2: capabilities now read workspace-only, NO approval written.
        workspace_only = self.seclib.Section(
            id="research", title="R", workflow=None, workflow_name="(inline)",
            default_mode="chat", artifact_types_emitted=[],
            artifact_types_accepted=[], dod_tier="standard",
            capabilities={"writes": "workspace", "exec": False,
                          "external": False})

        def mint(*a, **k):
            raise AssertionError("a pending route must NEVER mint without "
                                 "approval, even if caps flip to workspace-only")
        with unittest.mock.patch.object(cr, "load_route_config",
                                        lambda *a, **k: self.cfg), \
                unittest.mock.patch.object(self.artlib, "list_artifacts",
                                           lambda *a, **k: [self.meta]), \
                unittest.mock.patch.object(self.artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(
                    self.artlib, "lineage_index",
                    lambda *a, **k: {"by_id": {"a1": self.meta}}), \
                unittest.mock.patch.object(self.seclib, "load_section",
                                           lambda *a, **k: workspace_only), \
                unittest.mock.patch("sessions.mint_delegation_session", mint), \
                unittest.mock.patch("sessions.scan_effected_routes",
                                    lambda *a: set()):
            cond.route_engine(self.root, state, [self.sid], emit=lambda *a: None)
        # Still pending, still unapproved, never routed.
        self.assertTrue(cp.is_pending(self.root, rid))
        self.assertNotIn(rid, state["routed"])

    def test_failed_enqueue_ledgers_the_miss_not_a_false_request(self):
        state = cond.default_state()
        with unittest.mock.patch.object(cp, "enqueue_pending",
                                        lambda *a: False):
            self._run(state, lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("gated route must not mint")))
        recs = [r for r in cond.read_ledger(self.root) if r]
        self.assertTrue(any(r["decision"] == "enqueue_failed" for r in recs))
        self.assertFalse(any(r["decision"] == "approval_requested"
                             for r in recs))

    def test_malformed_pending_record_is_dropped_not_crashed(self):
        os.makedirs(cp._cdir(self.root), exist_ok=True)
        with open(cp.pending_path(self.root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"action_id": "bad"}) + "\n")   # no target/payload
        # A drain over the malformed record must not raise and must drop it.
        self._run(cond.default_state(), lambda *a, **k: "/d")
        self.assertFalse(cp.is_pending(self.root, "bad"))
