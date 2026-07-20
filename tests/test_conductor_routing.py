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
