"""V3 board 9.0: the Situation data format — loader round-trip, seed-then-
disk-wins, corrupt-file handling, the lint matrix, and the plan's gate
(every shipped seed validates against the REAL shipped doc_map.json)."""
import json
import os
import shutil
import tempfile
import unittest

import situations as sitlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)


def _quiet(*_a, **_k):
    pass


class _Base(unittest.TestCase):
    def setUp(self):
        self.orch_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.orch_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Loader + seeding
# --------------------------------------------------------------------------- #
class TestLoaderAndSeeding(_Base):
    def test_load_situation_materializes_and_returns_seeds(self):
        s = sitlib.load_situation("prototype_sprint", self.orch_dir,
                                  on_error=_quiet)
        self.assertIsNotNone(s)
        self.assertEqual(s["name"], "prototype_sprint")
        self.assertIn("problem_statement", s["doc_slots"])
        self.assertEqual(s["schema_version"], sitlib.SCHEMA_VERSION)

    def test_all_six_seeds_are_listed(self):
        names = sitlib.list_situations(self.orch_dir, on_error=_quiet)
        self.assertEqual(set(names), {
            "full_production_app", "prototype_sprint", "research_spike",
            "launch_push", "v2_iteration", "compliance_pass"})

    def test_seed_never_clobbers_a_user_edit(self):
        sitlib.ensure_seeded(self.orch_dir)
        path = os.path.join(sitlib.situations_root(self.orch_dir),
                            "prototype_sprint", "situation.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["description"] = "edited by the user"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        # a second ensure_seeded/load must NOT overwrite the edit
        s = sitlib.load_situation("prototype_sprint", self.orch_dir,
                                  on_error=_quiet)
        self.assertEqual(s["description"], "edited by the user")

    def test_round_trip_preserves_semantics(self):
        sitlib.ensure_seeded(self.orch_dir)
        path = os.path.join(sitlib.situations_root(self.orch_dir),
                            "research_spike", "situation.json")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        loaded = sitlib.load_situation("research_spike", self.orch_dir,
                                       on_error=_quiet)
        self.assertEqual(loaded["doc_slots"], raw["doc_slots"])
        self.assertEqual(loaded["pipeline_ref"], raw["pipeline_ref"])
        self.assertEqual(loaded["overrides"], raw["overrides"])

    def test_corrupt_file_reports_and_returns_none(self):
        sitlib.ensure_seeded(self.orch_dir)
        path = os.path.join(sitlib.situations_root(self.orch_dir),
                            "prototype_sprint", "situation.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        warned = []
        s = sitlib.load_situation("prototype_sprint", self.orch_dir,
                                  on_error=warned.append)
        self.assertIsNone(s)
        self.assertTrue(any("invalid JSON" in w for w in warned))

    def test_list_situations_skips_corrupt_but_keeps_the_rest(self):
        sitlib.ensure_seeded(self.orch_dir)
        path = os.path.join(sitlib.situations_root(self.orch_dir),
                            "launch_push", "situation.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not even json")
        warned = []
        names = sitlib.list_situations(self.orch_dir, on_error=warned.append)
        self.assertNotIn("launch_push", names)
        self.assertIn("prototype_sprint", names)
        self.assertTrue(warned)

    def test_missing_orch_dir_is_a_harmless_empty_list(self):
        shutil.rmtree(self.orch_dir)
        # ensure_seeded should still materialize it on first touch
        names = sitlib.list_situations(self.orch_dir, on_error=_quiet)
        self.assertTrue(len(names) >= 6)

    def test_non_object_root_is_refused(self):
        sitlib.ensure_seeded(self.orch_dir)
        path = os.path.join(sitlib.situations_root(self.orch_dir),
                            "v2_iteration", "situation.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(["not", "an", "object"], fh)
        warned = []
        s = sitlib.load_situation("v2_iteration", self.orch_dir,
                                  on_error=warned.append)
        self.assertIsNone(s)
        self.assertTrue(any("must be a JSON object" in w for w in warned))


# --------------------------------------------------------------------------- #
# Lint matrix — one fixture per problem class, names the field (§5.2)
# --------------------------------------------------------------------------- #
class TestLint(unittest.TestCase):
    DOC_MAP = {"slots": [
        {"slot_id": "problem_statement", "owner_section": "ideas"},
        {"slot_id": "target_user", "owner_section": "ideas"},
        {"slot_id": "market_landscape", "owner_section": "research"},
    ]}

    def _situation(self, **over):
        base = {"schema_version": 1, "name": "x", "description": "",
               "doc_slots": ["problem_statement"], "pipeline_ref": "",
               "overrides": {"sections": {}, "phases": {}, "casts": {}}}
        base.update(over)
        return base

    def test_valid_situation_has_zero_problems(self):
        problems = sitlib.lint_situation(self._situation(), self.DOC_MAP)
        self.assertEqual(problems, [])

    def test_unknown_slot_id_named(self):
        problems = sitlib.lint_situation(
            self._situation(doc_slots=["nonexistent_slot"]), self.DOC_MAP)
        self.assertTrue(any("nonexistent_slot" in p and "unknown slot id" in p
                            for p in problems))

    def test_duplicate_slot_named(self):
        problems = sitlib.lint_situation(
            self._situation(doc_slots=["problem_statement",
                                       "problem_statement"]), self.DOC_MAP)
        self.assertTrue(any("duplicate slot id" in p
                            and "problem_statement" in p for p in problems))

    def test_dangling_pipeline_ref_named(self):
        problems = sitlib.lint_situation(
            self._situation(pipeline_ref="ghost_pipeline"), self.DOC_MAP,
            presets=["brainstorm_to_plan"])
        self.assertTrue(any("ghost_pipeline" in p and "dangling" in p
                            for p in problems))

    def test_known_pipeline_ref_is_clean(self):
        problems = sitlib.lint_situation(
            self._situation(pipeline_ref="brainstorm_to_plan"), self.DOC_MAP,
            presets=["brainstorm_to_plan"])
        self.assertEqual(problems, [])

    def test_empty_pipeline_ref_never_dangles(self):
        problems = sitlib.lint_situation(
            self._situation(pipeline_ref=""), self.DOC_MAP, presets=[])
        self.assertEqual(problems, [])

    def test_unknown_section_named(self):
        problems = sitlib.lint_situation(
            self._situation(overrides={"sections": {"not_a_section": {}},
                                       "phases": {}, "casts": {}}),
            self.DOC_MAP)
        self.assertTrue(any("not_a_section" in p and "unknown section" in p
                            for p in problems))

    def test_known_section_from_doc_map_is_clean(self):
        problems = sitlib.lint_situation(
            self._situation(overrides={"sections": {"ideas": {"enabled": False}},
                                       "phases": {}, "casts": {}}),
            self.DOC_MAP)
        self.assertEqual(problems, [])

    def test_unknown_phase_override_field_named(self):
        problems = sitlib.lint_situation(
            self._situation(overrides={
                "sections": {}, "casts": {},
                "phases": {"tech_specs": {"bogus_field": "x"}}}),
            self.DOC_MAP)
        self.assertTrue(any("bogus_field" in p and "tech_specs" in p
                            for p in problems))

    def test_known_phase_field_and_rounds_are_clean(self):
        problems = sitlib.lint_situation(
            self._situation(overrides={
                "sections": {}, "casts": {},
                "phases": {"tech_specs": {"claude": True, "rounds": 2}}}),
            self.DOC_MAP)
        self.assertEqual(problems, [])

    def test_not_an_object_is_refused(self):
        problems = sitlib.lint_situation("not a dict", self.DOC_MAP)
        self.assertTrue(problems)

    def test_multiple_problems_all_reported_not_just_first(self):
        problems = sitlib.lint_situation(
            self._situation(doc_slots=["nope1", "nope2"]), self.DOC_MAP)
        self.assertEqual(len(problems), 2)


# --------------------------------------------------------------------------- #
# The plan's gate: every seed validates against the REAL shipped doc_map.json
# --------------------------------------------------------------------------- #
class TestSeedsValidateAgainstShippedDocMap(_Base):
    def test_every_seed_passes_lint_with_zero_problems(self):
        doc_map_path = os.path.join(REPO_ROOT, "sections", "documentation",
                                    "doc_map.json")
        with open(doc_map_path, encoding="utf-8") as fh:
            doc_map = json.load(fh)
        names = sitlib.list_situations(self.orch_dir, on_error=_quiet)
        self.assertEqual(len(names), 6)
        for name in names:
            situation = sitlib.load_situation(name, self.orch_dir,
                                              on_error=_quiet)
            problems = sitlib.lint_situation(situation, doc_map, presets=[])
            self.assertEqual(problems, [],
                             "seed %r has lint problems: %r" % (name, problems))

    def test_full_production_app_covers_every_shipped_slot(self):
        doc_map_path = os.path.join(REPO_ROOT, "sections", "documentation",
                                    "doc_map.json")
        with open(doc_map_path, encoding="utf-8") as fh:
            doc_map = json.load(fh)
        shipped_ids = {s["slot_id"] for s in doc_map["slots"]}
        situation = sitlib.load_situation("full_production_app",
                                          self.orch_dir, on_error=_quiet)
        self.assertEqual(set(situation["doc_slots"]), shipped_ids)

    def test_overrides_sections_reference_only_real_owner_sections(self):
        doc_map_path = os.path.join(REPO_ROOT, "sections", "documentation",
                                    "doc_map.json")
        with open(doc_map_path, encoding="utf-8") as fh:
            doc_map = json.load(fh)
        real_sections = {s["owner_section"] for s in doc_map["slots"]}
        for name in sitlib.list_situations(self.orch_dir, on_error=_quiet):
            situation = sitlib.load_situation(name, self.orch_dir,
                                              on_error=_quiet)
            for sec in situation["overrides"]["sections"]:
                self.assertIn(sec, real_sections)


# --------------------------------------------------------------------------- #
# No engine coupling — data layer only (acceptance criterion)
# --------------------------------------------------------------------------- #
class TestNoEngineCoupling(unittest.TestCase):
    def test_situations_module_does_not_import_orchestrator_or_conductor(self):
        import ast
        path = os.path.join(REPO_ROOT, "situations.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertNotIn("orchestrator", imported)
        self.assertNotIn("conductor", imported)


if __name__ == "__main__":
    unittest.main()
