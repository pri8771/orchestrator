"""V3 7.3: route idempotency — deterministic route_id, ledger-before-act
intent->done, and restart dedup (same content re-dedups, new version
re-routes; recovery probe skips a re-mint the effect already landed)."""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import conductor as cond
import conductor_routing as cr
import sessions as seslib


class TestRouteIdDeterminism(unittest.TestCase):
    def test_same_inputs_same_id_new_content_new_id(self):
        k1 = {"artifact_id": "a", "content_hash": "h1", "target": "research",
              "rule_id": "r"}
        k2 = dict(k1)   # identical
        self.assertEqual(cr.route_id_for(k1), cr.route_id_for(k2))
        k3 = dict(k1, content_hash="h2")   # new version
        self.assertNotEqual(cr.route_id_for(k1), cr.route_id_for(k3))
        k4 = dict(k1, target="planning")   # different target
        self.assertNotEqual(cr.route_id_for(k1), cr.route_id_for(k4))
        k5 = dict(k1, rule_id="r2")        # different rule
        self.assertNotEqual(cr.route_id_for(k1), cr.route_id_for(k5))

    def test_conductor_route_digest_delegates(self):
        k = {"artifact_id": "a", "content_hash": "h", "target": "t",
             "rule_id": "r"}
        self.assertEqual(cond.route_digest(k), cr.route_id_for(k))

    def test_intent_exposes_route_id_and_marker(self):
        intent = cr.RouteIntent("a", "h", "ideas", "research", "one", "r",
                                cr.ALLOW)
        self.assertEqual(intent.route_id,
                         cr.route_id_for(intent.route_key))
        self.assertIn(intent.route_id, cr.route_marker(intent.route_id))


class TestLedgerBeforeActProtocol(unittest.TestCase):
    def _run(self, mint, probe=None):
        lines = []
        intent = cr.RouteIntent("a1", "h1", "ideas", "research", "one", "r",
                                cr.ALLOW)
        outs = cr.execute_intents([intent], "proj/ideas/chat-1", "/root",
                                  mint, lines.append, probe=probe)
        return lines, outs, intent

    def test_route_id_and_content_hash_reach_the_mint_request(self):
        captured = {}
        def mint(target, req):
            captured.update(req)
            return "/d"
        lines, outs, intent = self._run(mint)
        self.assertEqual(captured["route_id"], intent.route_id)
        self.assertEqual(captured["content_hash"], "h1")
        self.assertIn(cr.route_marker(intent.route_id), captured["title"])

    def test_every_ledger_line_carries_route_id(self):
        lines, outs, intent = self._run(lambda *a: "/d")
        self.assertTrue(lines)
        self.assertTrue(all(l["route_id"] == intent.route_id for l in lines))
        self.assertEqual([l["decision"] for l in lines],
                         ["route_proposed", "route_approved"])

    def test_probe_recovery_skips_mint_and_ledgers_recovered(self):
        def mint(*a):
            raise AssertionError("recovered route must not re-mint")
        lines, outs, intent = self._run(mint, probe=lambda rid, tgt: True)
        self.assertEqual(outs[0]["outcome"], "recovered")
        self.assertEqual([l["decision"] for l in lines],
                         ["route_proposed", "route_recovered"])


class TestRestartDedup(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(self.root, self.sid))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _route_once(self, state, content_hash, mint):
        import artifacts as artlib
        cfg = cr.RouteConfig(routes={"idea": "research"})
        meta = {"id": "a1", "artifact_type": "idea",
                "content_hash": content_hash, "lineage": [], "hop_count": 0,
                "status": "final"}
        with unittest.mock.patch.object(cr, "load_route_config",
                                        lambda *a, **k: cfg), \
                unittest.mock.patch.object(artlib, "list_artifacts",
                                           lambda *a, **k: [meta]), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(
                    artlib, "lineage_index",
                    lambda *a, **k: {"by_id": {"a1": meta}}), \
                unittest.mock.patch("sessions.mint_delegation_session", mint), \
                unittest.mock.patch(
                    "sessions.scan_effected_routes", lambda *a: set()):
            cond.route_engine(self.root, state, [self.sid], emit=lambda *a: None)

    def test_new_version_mints_distinct_route(self):
        mints = []
        def mint(root, project, section, req, **kw):
            mints.append(req["route_id"])
            return os.path.join(root, project, section, req["route_id"][:6])
        state = cond.default_state()
        self._route_once(state, "h1", mint)   # version 1
        self._route_once(state, "h1", mint)   # same version -> deduped
        self._route_once(state, "h2", mint)   # NEW version -> re-routes
        self.assertEqual(len(mints), 2, "same version once, new version once")
        self.assertNotEqual(mints[0], mints[1])   # distinct route_ids

    def test_persisted_routed_survives_reload(self):
        def mint(root, project, section, req, **kw):
            return os.path.join(root, project, section, "x")
        state = cond.default_state()
        self._route_once(state, "h1", mint)
        # A fresh conductor loads the persisted routed-set and won't re-route.
        reloaded = cond.load_conductor_state(self.root)
        self.assertTrue(reloaded["routed"])
        mints = []
        def mint2(root, project, section, req, **kw):
            mints.append(1)
            return "/d"
        self._route_once(reloaded, "h1", mint2)
        self.assertEqual(mints, [], "persisted routed-set dedups after reload")


class TestProbeProvenance(unittest.TestCase):
    def test_scan_indexes_route_id_and_legacy_key(self):
        root = tempfile.mkdtemp()
        try:
            base = os.path.join(root, "proj", "research")
            # a 7.3 record (route_id) and a pre-7.3 legacy record (no route_id)
            c1 = os.path.join(base, "chat-new"); os.makedirs(c1)
            seslib._write_delegation(c1, seslib._new_record(
                "did1", {"route_id": "rid-abc", "artifact_id": "a1",
                         "rule_id": "r1"}, None, None))
            c2 = os.path.join(base, "chat-old"); os.makedirs(c2)
            seslib._write_delegation(c2, seslib._new_record(
                "did2", {"artifact_id": "a9", "rule_id": "r9"},  # no route_id
                None, None))
            effected = seslib.scan_effected_routes(root, "proj", "research")
            self.assertIn("rid-abc", effected)
            self.assertIn(("legacy", "a1", "r1"), effected)
            self.assertIn(("legacy", "a9", "r9"), effected)   # migration key
            self.assertNotIn("rid-other", effected)
            self.assertEqual(
                seslib.scan_effected_routes(root, "proj", "absent"), set())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_legacy_session_prevents_duplicate_mint_across_boundary(self):
        # The 7.2b->7.3 migration case: a session minted before 7.3 (no
        # route_id) must be recovered, not re-minted as a duplicate.
        root = tempfile.mkdtemp()
        sid = "proj/ideas/chat-1"
        os.makedirs(os.path.join(root, sid))
        try:
            import artifacts as artlib
            meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
                    "lineage": [], "hop_count": 0, "status": "final"}
            cfg = cr.RouteConfig(routes={"idea": "research"})
            intent = cr.plan_routes(meta, "ideas", cfg, {})[0]
            # A pre-7.3 delegation already exists for this route's legacy key.
            legacy_chat = os.path.join(root, "proj", "research", "chat-old")
            os.makedirs(legacy_chat)
            seslib._write_delegation(legacy_chat, seslib._new_record(
                "old", {"artifact_id": intent.artifact_id,
                        "rule_id": intent.rule_id}, None, None))

            def mint(*a, **k):
                raise AssertionError("legacy route must recover, not re-mint")

            with unittest.mock.patch.object(cr, "load_route_config",
                                            lambda *a, **k: cfg), \
                    unittest.mock.patch.object(artlib, "list_artifacts",
                                               lambda *a, **k: [meta]), \
                    unittest.mock.patch.object(artlib, "is_admissible",
                                               lambda *a, **k: True), \
                    unittest.mock.patch.object(
                        artlib, "lineage_index",
                        lambda *a, **k: {"by_id": {"a1": meta}}), \
                    unittest.mock.patch("sessions.mint_delegation_session",
                                        mint):
                cond.route_engine(root, cond.default_state(), [sid],
                                  emit=lambda *a: None)
            recs = [r for r in cond.read_ledger(root) if r]
            self.assertTrue(any(r["decision"] == "route_recovered"
                                for r in recs))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
