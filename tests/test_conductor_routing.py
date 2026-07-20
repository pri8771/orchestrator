"""V3 7.2 sub-PR (a): routing rule loading, two-layer merge, validation,
deterministic resolution, and the pure guard (whole-lineage convergence +
hop budget). Side-effect-free — no minting here (sub-PR b).
"""
import json
import os
import shutil
import tempfile
import unittest

import conductor_routing as cr


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sections = os.path.join(self.root, "sections")
        os.makedirs(os.path.join(self.sections, "ideas"))
        self.project = os.path.join(self.root, "proj")
        os.makedirs(self.project)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_section(self, obj):
        with open(os.path.join(self.sections, "ideas", "routing.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def _write_project(self, obj):
        with open(os.path.join(self.project, "routing.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def _load(self, warn=None):
        return cr.load_route_config(self.sections, "ideas", self.project,
                                    on_warn=warn)


class TestMergePrecedence(_Base):
    def test_project_overrides_section_non_empty_wins(self):
        self._write_section({"artifact_routes": {"idea": "research",
                                                 "gap": "documentation"}})
        self._write_project({"artifact_routes": {"idea": "planning",
                                                 "gap": ""}})
        cfg = self._load()
        self.assertTrue(cfg.ok)
        # project's non-empty 'idea' wins; its blank 'gap' inherits section.
        self.assertEqual(cfg.routes["idea"], "planning")
        self.assertEqual(cfg.routes["gap"], "documentation")

    def test_absent_layers_are_empty_not_error(self):
        cfg = self._load()
        self.assertTrue(cfg.ok)
        self.assertEqual(cfg.routes, {})
        self.assertEqual(cfg.rules, [])

    def test_model_routing_shaped_file_yields_no_routes(self):
        # A real model_routing.json overlay carries phases/enabled/fallback
        # and NO artifact_routes — it must not produce a route.
        self._write_section({"schema_version": 1, "enabled": True,
                             "fallback": "claude",
                             "phases": {"tech_specs": {"claude": "x"}}})
        cfg = self._load()
        self.assertTrue(cfg.ok)
        self.assertEqual(cfg.routes, {})

    def test_rule_array_shape_normalizes(self):
        self._write_section({"artifact_routes": [
            {"match": {"artifact_type": "idea"}, "target": "research"}]})
        cfg = self._load()
        self.assertEqual(cfg.routes["idea"], "research")


class TestCorruptConfig(_Base):
    def test_corrupt_file_disables_with_banner(self):
        with open(os.path.join(self.sections, "ideas", "routing.json"),
                  "w", encoding="utf-8") as fh:
            fh.write("{not json")
        warns = []
        cfg = self._load(warn=warns.append)
        self.assertFalse(cfg.ok)
        self.assertIn("routing disabled", cfg.banner)
        self.assertTrue(warns)

    def test_non_object_file_disables(self):
        self._write_section([1, 2, 3])
        self.assertFalse(self._load().ok)


class TestRuleValidation(_Base):
    def test_bad_strategy_dropped_with_specific_warning(self):
        warns = []
        self._write_section({"rules": [
            {"match": {"artifact_type": "idea"}, "strategy": "teleport",
             "targets": ["research"]},
            {"match": {"artifact_type": "gap"}, "strategy": "one",
             "targets": ["documentation"]}]})
        cfg = self._load(warn=warns.append)
        self.assertTrue(cfg.ok)   # a bad RULE doesn't disable the FILE
        self.assertEqual(len(cfg.rules), 1)
        self.assertEqual(cfg.rules[0]["artifact_type"], "gap")
        self.assertTrue(any("teleport" in w for w in warns))

    def test_missing_targets_and_type_and_budget(self):
        for bad, needle in (
            ({"match": {}, "strategy": "one", "targets": ["x"]}, "artifact_type"),
            ({"match": {"artifact_type": "i"}, "targets": []}, "no targets"),
            ({"match": {"artifact_type": "i"}, "targets": ["x"],
              "hop_budget": 0}, "hop_budget"),
        ):
            rule, err = cr.validate_rule(bad)
            self.assertIsNone(rule)
            self.assertIn(needle, err)

    def test_target_singular_promoted_and_rule_id_stable(self):
        rule, err = cr.validate_rule(
            {"match": {"artifact_type": "idea"}, "target": "research"})
        self.assertIsNone(err)
        self.assertEqual(rule["targets"], ["research"])
        again, _ = cr.validate_rule(
            {"match": {"artifact_type": "idea"}, "target": "research"})
        self.assertEqual(rule["rule_id"], again["rule_id"])   # deterministic


class TestResolutionAndRules(_Base):
    def test_deterministic_target_and_classifier_signal(self):
        self._write_section({"artifact_routes": {"idea": "research"}})
        cfg = self._load()
        self.assertEqual(cr.deterministic_target("idea", cfg), "research")
        # unmapped type -> None -> the ONLY case that may invoke a classifier
        self.assertIsNone(cr.deterministic_target("mystery", cfg))

    def test_rules_for_respects_source_section(self):
        self._write_section({"rules": [
            {"match": {"artifact_type": "idea"}, "targets": ["research"]},
            {"match": {"artifact_type": "idea", "source_section": "ideas"},
             "targets": ["planning"]},
            {"match": {"artifact_type": "idea", "source_section": "qa"},
             "targets": ["nope"]}]})
        cfg = self._load()
        applicable = cr.rules_for("idea", "ideas", cfg)
        self.assertEqual(len(applicable), 2)   # the qa-scoped one excluded
        self.assertNotIn("nope",
                         [t for r in applicable for t in r["targets"]])


class TestGuard(unittest.TestCase):
    def test_allow_on_clean_lineage(self):
        meta = {"id": "a2", "content_hash": "h2", "lineage": ["a1"],
                "hop_count": 1}
        anc = {"a1": {"content_hash": "h1"}}
        self.assertEqual(cr.guard_route(meta, anc, hop_budget=4), cr.ALLOW)

    def test_oscillation_two_hops_back_is_converged(self):
        # A -> B -> A': A'.content_hash == A.content_hash, but B differs, so
        # artifacts.py's direct-parent converged check missed it. The
        # whole-lineage scan catches it here.
        aprime = {"id": "a3", "content_hash": "hA",
                  "lineage": ["a1", "a2"], "hop_count": 2}
        ancestors = {"a1": {"content_hash": "hA"},   # the original A
                     "a2": {"content_hash": "hB"}}   # B, different body
        self.assertEqual(cr.guard_route(aprime, ancestors, hop_budget=9),
                         cr.CONVERGED)

    def test_hop_budget_exhaustion(self):
        meta = {"id": "x", "content_hash": "h", "lineage": ["p"],
                "hop_count": 4}
        self.assertEqual(cr.guard_route(meta, {"p": {"content_hash": "o"}},
                                        hop_budget=4), cr.BUDGET_EXHAUSTED)

    def test_missing_ancestor_hash_fails_open_not_converged(self):
        # We can't PROVE a collision without the ancestor's hash; a real
        # collision always has it, so absence must not block a legit route.
        meta = {"id": "x", "content_hash": "h", "lineage": ["p"],
                "hop_count": 0}
        self.assertEqual(cr.guard_route(meta, {}, hop_budget=4), cr.ALLOW)

    def test_nonsense_meta_never_routes(self):
        self.assertEqual(cr.guard_route("not-a-dict", {}), cr.CONVERGED)


if __name__ == "__main__":
    unittest.main()


class TestPlanRoutes(unittest.TestCase):
    def _cfg(self, routes=None, rules=None):
        return cr.RouteConfig(routes=routes or {}, rules=rules or [])

    def _meta(self, atype="idea", aid="a1", chash="h1", lineage=None,
              hop=0):
        return {"id": aid, "artifact_type": atype, "content_hash": chash,
                "lineage": lineage or [], "hop_count": hop}

    def test_deterministic_one_never_calls_classifier(self):
        calls = []
        cfg = self._cfg(routes={"idea": "research"})
        intents = cr.plan_routes(self._meta("idea"), "ideas", cfg, {},
                                 classify=lambda *a: calls.append(a) or "x")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].target, "research")
        self.assertEqual(intents[0].verdict, cr.ALLOW)
        self.assertEqual(calls, [], "mapped type must not invoke classifier")

    def test_unmapped_type_invokes_classifier_once_closed_list(self):
        calls = []
        cfg = self._cfg(routes={"idea": "research", "gap": "documentation"})

        def classify(atype, candidates):
            calls.append((atype, tuple(candidates)))
            return "documentation"

        intents = cr.plan_routes(self._meta("mystery"), "ideas", cfg, {},
                                 classify=classify)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], ("documentation", "research"))  # closed
        self.assertEqual(intents[0].target, "documentation")

    def test_classifier_declining_yields_unroutable_not_a_guess(self):
        cfg = self._cfg(routes={"idea": "research"})
        intents = cr.plan_routes(self._meta("mystery"), "ideas", cfg, {},
                                 classify=lambda *a: "not-a-section")
        self.assertEqual(intents[0].verdict, "unroutable")
        self.assertIsNone(intents[0].target)

    def test_broadcast_fans_out_one_intent_per_target(self):
        cfg = self._cfg(rules=[{"artifact_type": "idea", "source_section": None,
                                "strategy": "every",
                                "targets": ["research", "planning", "qa"],
                                "hop_budget": 4, "rule_id": "r1"}])
        intents = cr.plan_routes(self._meta("idea"), "ideas", cfg, {})
        self.assertEqual(sorted(i.target for i in intents),
                         ["planning", "qa", "research"])
        self.assertTrue(all(i.verdict == cr.ALLOW for i in intents))

    def test_chain_stops_at_hop_budget(self):
        cfg = self._cfg(rules=[{"artifact_type": "idea", "source_section": None,
                                "strategy": "chain", "targets": ["research"],
                                "hop_budget": 2, "rule_id": "rc"}])
        deep = self._meta("idea", hop=2)   # already at budget
        intents = cr.plan_routes(deep, "ideas", cfg, {})
        self.assertEqual(intents[0].verdict, cr.BUDGET_EXHAUSTED)

    def test_oscillation_plans_a_converged_intent(self):
        cfg = self._cfg(routes={"idea": "research"})
        aprime = self._meta("idea", aid="a3", chash="hA",
                            lineage=["a1", "a2"], hop=2)
        anc = {"a1": {"content_hash": "hA"}, "a2": {"content_hash": "hB"}}
        intents = cr.plan_routes(aprime, "ideas", cfg, anc)
        self.assertEqual(intents[0].verdict, cr.CONVERGED)


class TestExecuteIntents(unittest.TestCase):
    def _ledger(self):
        lines = []
        return lines, lines.append

    def _allow_intent(self, target="research"):
        return cr.RouteIntent("a1", "h1", "ideas", target, "one", "r1",
                              cr.ALLOW)

    def test_ledger_before_act_and_mint_called(self):
        lines, ledger = self._ledger()
        minted = []
        def mint(target, req):
            # route_proposed must already be on the ledger BEFORE we mint.
            self.assertTrue(any(l["decision"] == "route_proposed"
                                for l in lines))
            minted.append((target, req["artifact_id"]))
            return "/root/proj/research/chat-x"
        out = cr.execute_intents([self._allow_intent()], "proj", "/root",
                                 mint, ledger)
        self.assertEqual(minted, [("research", "a1")])
        self.assertEqual(out[0]["outcome"], "routed")
        decisions = [l["decision"] for l in lines]
        self.assertEqual(decisions, ["route_proposed", "route_approved"])

    def test_guarded_intent_never_mints(self):
        lines, ledger = self._ledger()
        conv = cr.RouteIntent("a1", "hA", "ideas", "research", "one", "r1",
                              cr.CONVERGED, "converged")
        def mint(*a):
            raise AssertionError("a converged route must never mint")
        out = cr.execute_intents([conv], "proj", "/root", mint, ledger)
        self.assertEqual(out[0]["outcome"], "converged")
        self.assertIn("converged", [l["decision"] for l in lines])

    def test_permit_seam_denies_without_minting(self):
        lines, ledger = self._ledger()
        def mint(*a):
            raise AssertionError("a denied route must never mint")
        out = cr.execute_intents([self._allow_intent()], "proj", "/root",
                                 mint, ledger, permit=lambda _i: False)
        self.assertEqual(out[0]["outcome"], "denied")
        self.assertIn("denied", [l["decision"] for l in lines])

    def test_toctou_idempotent_mint_yields_one_session(self):
        # Two evaluation passes over the same artifact: the injected mint is
        # idempotent (like sessions.mint_delegation_session), so the loser
        # adopts the same dir — exactly one real session, both ledgered.
        lines, ledger = self._ledger()
        seen = {}
        def mint(target, req):
            key = (target, req["artifact_id"])
            seen.setdefault(key, "/root/proj/%s/chat-fixed" % target)
            return seen[key]
        cr.execute_intents([self._allow_intent()], "proj", "/root", mint, ledger)
        cr.execute_intents([self._allow_intent()], "proj", "/root", mint, ledger)
        self.assertEqual(len(seen), 1)   # one session dir across both passes
        approved = [l for l in lines if l["decision"] == "route_approved"]
        self.assertEqual(len(approved), 2)   # both passes ledgered honestly

    def test_mint_failure_is_ledgered_not_silent(self):
        lines, ledger = self._ledger()
        out = cr.execute_intents([self._allow_intent()], "proj", "/root",
                                 lambda *a: None, ledger)
        self.assertEqual(out[0]["outcome"], "mint_failed")
        self.assertIn("mint_failed", [l["decision"] for l in lines])

    def test_route_key_carries_7_3_hash_inputs(self):
        lines, ledger = self._ledger()
        cr.execute_intents([self._allow_intent()], "proj", "/root",
                           lambda *a: "/d", ledger)
        rk = lines[0]["route_key"]
        self.assertEqual(set(rk), {"artifact_id", "content_hash", "target",
                                   "rule_id"})


class TestReviewFixes(unittest.TestCase):
    """Regressions for the 7.2b adversarial review."""

    def test_project_rule_overrides_section_rule_same_type(self):
        # Not blind concatenation: a project rule for a type REPLACES the
        # section rule, mirroring the routes map's precedence.
        root = tempfile.mkdtemp()
        try:
            secdir = os.path.join(root, "sections", "ideas")
            os.makedirs(secdir)
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            with open(os.path.join(secdir, "routing.json"), "w") as fh:
                json.dump({"rules": [{"match": {"artifact_type": "idea"},
                                      "strategy": "one",
                                      "targets": ["research"]}]}, fh)
            with open(os.path.join(proj, "routing.json"), "w") as fh:
                json.dump({"rules": [{"match": {"artifact_type": "idea"},
                                      "strategy": "one",
                                      "targets": ["planning"]}]}, fh)
            cfg = cr.load_route_config(os.path.join(root, "sections"),
                                       "ideas", proj)
            rules = cr.rules_for("idea", "ideas", cfg)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0]["targets"], ["planning"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_one_and_chain_reject_multiple_targets(self):
        for strat in ("one", "chain"):
            rule, err = cr.validate_rule(
                {"match": {"artifact_type": "idea"}, "strategy": strat,
                 "targets": ["a", "b"]})
            self.assertIsNone(rule)
            self.assertIn("single destination", err)
        ok, err = cr.validate_rule(
            {"match": {"artifact_type": "idea"}, "strategy": "every",
             "targets": ["a", "b"]})
        self.assertIsNone(err)   # every legitimately fans out

    def test_classifier_candidates_include_rule_targets(self):
        seen = {}
        cfg = cr.RouteConfig(
            routes={},   # NO flat routes — all destinations via rules
            rules=[{"artifact_type": "idea", "source_section": None,
                    "strategy": "one", "targets": ["planning"],
                    "hop_budget": 4, "rule_id": "r"}])
        meta = {"id": "a1", "artifact_type": "mystery", "content_hash": "h",
                "lineage": [], "hop_count": 0}

        def classify(atype, candidates):
            seen["candidates"] = tuple(candidates)
            return "planning"

        cr.plan_routes(meta, "ideas", cfg, {}, classify=classify)
        self.assertIn("planning", seen["candidates"])
