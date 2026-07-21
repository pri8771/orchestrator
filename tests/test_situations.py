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


# --------------------------------------------------------------------------- #
# V3 board 9.1a: resolve_required_slots (pure, unit-testable without the
# engine)
# --------------------------------------------------------------------------- #
class TestResolveRequiredSlots(unittest.TestCase):
    DOC_MAP = {"slots": [
        {"slot_id": "problem_statement", "owner_section": "ideas"},
        {"slot_id": "target_user", "owner_section": "ideas"},
        {"slot_id": "market_landscape", "owner_section": "research"},
    ]}

    def test_resolves_ordered_slots_and_owning_sections(self):
        situation = {"doc_slots": ["market_landscape", "problem_statement"]}
        slots, owners = sitlib.resolve_required_slots(situation, self.DOC_MAP)
        self.assertEqual(slots, ["market_landscape", "problem_statement"])
        self.assertEqual(owners, {"research", "ideas"})

    def test_unknown_slot_id_is_silently_dropped_not_raised(self):
        situation = {"doc_slots": ["problem_statement", "ghost_slot"]}
        slots, owners = sitlib.resolve_required_slots(situation, self.DOC_MAP)
        self.assertEqual(slots, ["problem_statement"])
        self.assertEqual(owners, {"ideas"})

    def test_duplicate_slot_ids_deduplicated(self):
        situation = {"doc_slots": ["problem_statement", "problem_statement"]}
        slots, _owners = sitlib.resolve_required_slots(situation, self.DOC_MAP)
        self.assertEqual(slots, ["problem_statement"])

    def test_none_situation_is_a_harmless_empty_result(self):
        self.assertEqual(sitlib.resolve_required_slots(None, self.DOC_MAP),
                         ([], set()))

    def test_empty_doc_slots_is_empty_result(self):
        self.assertEqual(
            sitlib.resolve_required_slots({"doc_slots": []}, self.DOC_MAP),
            ([], set()))

    def test_every_seed_resolves_cleanly_against_the_shipped_doc_map(self):
        doc_map_path = os.path.join(REPO_ROOT, "sections", "documentation",
                                    "doc_map.json")
        with open(doc_map_path, encoding="utf-8") as fh:
            doc_map = json.load(fh)
        tmp = tempfile.mkdtemp()
        try:
            for name in sitlib.list_situations(tmp, on_error=_quiet):
                situation = sitlib.load_situation(name, tmp, on_error=_quiet)
                slots, owners = sitlib.resolve_required_slots(situation, doc_map)
                self.assertEqual(slots, situation["doc_slots"],
                                 "every seed slot is a real, resolvable id")
                self.assertTrue(owners or not slots)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# V3 board 9.1a: filter_phases_by_slots — the plan's phase-filter matrix gate.
# Fixture workflows (real workflows.Phase objects WITH doc_sections
# populated) prove the intersection/fallback MECHANISM works; a separate
# test against the REAL shipped workflow documents today's honest limit (no
# shipped workflow populates doc_sections yet — see completeness.
# filter_phases_by_slots' docstring).
# --------------------------------------------------------------------------- #
class TestFilterPhasesBySlots(unittest.TestCase):
    def setUp(self):
        import completeness
        import workflows as wf
        self.complib = completeness
        self.wf = wf
        self.phases = [
            wf.Phase("research", ".", "r.md", "p",
                    doc_sections=["market_landscape"]),
            wf.Phase("design", ".", "d.md", "p",
                    doc_sections=["design_language"]),
            wf.Phase("build", ".", "b.md", "p",
                    doc_sections=["technical_architecture", "build_plan"]),
            wf.Phase("qa", ".", "q.md", "p", doc_sections=["test_plan"]),
            wf.Phase("final_review", ".", "f.md", "p", doc_sections=[]),
        ]

    def test_no_required_slots_is_a_no_op(self):
        self.assertEqual(
            self.complib.filter_phases_by_slots(self.phases, None), self.phases)
        self.assertEqual(
            self.complib.filter_phases_by_slots(self.phases, []), self.phases)

    def test_keeps_only_phases_whose_doc_sections_intersect(self):
        kept = self.complib.filter_phases_by_slots(
            self.phases, {"market_landscape", "design_language"})
        keys = [self.wf.phase_key(p) for p in kept]
        # research + design match; final_review always kept (structural);
        # build/qa don't match and aren't structural -> excluded.
        self.assertEqual(set(keys), {"research", "design", "final_review"})

    def test_final_phase_always_kept_even_with_zero_overlap(self):
        # 3 non-final matches (>= _MIN_KEPT_PHASES, so the fallback does NOT
        # trigger) but the FINAL phase's own doc_sections don't match
        # anything — it must still be force-included by the explicit
        # retention rule, isolated from the separate fallback guarantee.
        kept = self.complib.filter_phases_by_slots(
            self.phases, {"market_landscape", "design_language", "test_plan"})
        keys = [self.wf.phase_key(p) for p in kept]
        self.assertIn("final_review", keys)
        self.assertEqual(set(keys), {"research", "design", "qa",
                                     "final_review"})

    def test_min_kept_fallback_triggers_when_too_few_match(self):
        kept = self.complib.filter_phases_by_slots(
            self.phases, {"nonexistent_slot_that_matches_nothing"})
        # only final_review would match structurally; MIN_KEPT_PHASES
        # fallback should trigger since 1 < _MIN_KEPT_PHASES -> ALL phases.
        self.assertEqual(kept, self.phases)

    def test_gutting_filter_falls_back_to_all_phases_with_warning(self):
        warned = []
        kept = self.complib.filter_phases_by_slots(
            self.phases, {"market_landscape"}, on_warn=warned.append)
        # only research + final_review match -> 2 kept, below
        # _MIN_KEPT_PHASES (3) -> fallback to ALL phases.
        self.assertEqual(kept, self.phases)
        self.assertTrue(any("running ALL phases" in w for w in warned))

    def test_wide_match_keeps_exactly_the_matching_subset(self):
        kept = self.complib.filter_phases_by_slots(
            self.phases, {"market_landscape", "design_language",
                         "technical_architecture", "test_plan"})
        keys = [self.wf.phase_key(p) for p in kept]
        self.assertEqual(set(keys), {"research", "design", "build", "qa",
                                     "final_review"})
        self.assertEqual(len(kept), len(self.phases))  # order preserved too
        self.assertEqual(kept, self.phases)

    def test_order_is_preserved(self):
        kept = self.complib.filter_phases_by_slots(
            self.phases, {"market_landscape", "test_plan"})
        keys = [self.wf.phase_key(p) for p in kept]
        self.assertEqual(keys, ["research", "qa", "final_review"])


class TestFilterPhasesBySlotsAgainstShippedWorkflow(unittest.TestCase):
    """Documents today's honest drift: no shipped workflow populates
    doc_sections, so applying any situation's required slots to it currently
    falls back to ALL phases via the _MIN_KEPT_PHASES guarantee — the SAFE
    behavior (an all-empty allow-list is exactly what the guarantee exists
    to protect against), not a bug in this function."""

    def test_applying_a_seed_to_app_build_returns_all_phases_today(self):
        import completeness
        import workflows as wf
        workflow = wf.load_workflow("app_build", REPO_ROOT)
        tmp = tempfile.mkdtemp()
        try:
            situation = sitlib.load_situation("prototype_sprint", tmp,
                                              on_error=_quiet)
            doc_map_path = os.path.join(REPO_ROOT, "sections", "documentation",
                                        "doc_map.json")
            with open(doc_map_path, encoding="utf-8") as fh:
                doc_map = json.load(fh)
            slots, _owners = sitlib.resolve_required_slots(situation, doc_map)
            self.assertTrue(slots)   # the seed genuinely has required slots
            warned = []
            kept = completeness.filter_phases_by_slots(
                workflow.phases, slots, on_warn=warned.append)
            self.assertEqual(kept, workflow.phases)
            self.assertTrue(warned, "the fallback banner fires — this is the "
                                    "safe, visible degradation, not silence")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# V3 board 9.1a: engine wiring — drives the REAL orchestrator._apply_situation
# _ref (extracted specifically so this doesn't have to invoke the entire
# pipeline), not just its constituent pieces in isolation. Patches HERE so
# sitlib.load_situation/docslib.load_doc_map read from a real tmp fixture,
# not the actual repo's fleet situations/.
# --------------------------------------------------------------------------- #
class TestApplySituationRef(unittest.TestCase):
    def setUp(self):
        import orchestrator as orch
        import turncontext as tcxlib
        self.orch = orch
        self.fleet_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.fleet_dir, ignore_errors=True)
        self._orig_here = orch.HERE
        orch.HERE = self.fleet_dir
        self.addCleanup(self._restore_here)
        self.tctx = tcxlib.TurnContext({})
        self.wf = __import__("workflows")
        self.phases = [
            self.wf.Phase("research", ".", "r.md", "p",
                          doc_sections=["market_landscape"]),
            self.wf.Phase("design", ".", "d.md", "p",
                          doc_sections=["design_language"]),
            self.wf.Phase("build", ".", "b.md", "p",
                          doc_sections=["technical_architecture"]),
            self.wf.Phase("final_review", ".", "f.md", "p", doc_sections=[]),
        ]

    def _restore_here(self):
        self.orch.HERE = self._orig_here

    def test_no_ref_returns_phases_unchanged_and_resets_tctx(self):
        # the ACTUAL "byte-identical for ref-less runs" claim: `phases` is
        # the SAME object back (nothing rebuilt/reordered), and any stale
        # prior-app state on tctx is explicitly reset to None.
        self.tctx.situation_name = "leftover_from_a_prior_app_in_this_cfg"
        self.tctx.required_slots = ["leftover"]
        out = self.orch._apply_situation_ref(self.tctx, {}, self.phases)
        self.assertIs(out, self.phases)
        self.assertIsNone(self.tctx.situation_name)
        self.assertIsNone(self.tctx.required_slots)

    def test_unknown_ref_bannners_and_leaves_phases_and_tctx_untouched(self):
        out = self.orch._apply_situation_ref(
            self.tctx, {"situation": "totally_made_up_name"}, self.phases)
        self.assertIs(out, self.phases)
        self.assertIsNone(self.tctx.situation_name)
        self.assertIsNone(self.tctx.required_slots)

    def test_real_ref_filters_phases_and_sets_tctx(self):
        sitlib.ensure_seeded(self.fleet_dir)   # materialize the fleet seeds
        out = self.orch._apply_situation_ref(
            self.tctx, {"situation": "research_spike"}, self.phases)
        # research_spike's slots include market_landscape (matches
        # 'research') but not design_language/technical_architecture — with
        # only 1 non-final match, _MIN_KEPT_PHASES fallback returns ALL of
        # the phases THIS call was given.
        self.assertEqual(out, self.phases)
        self.assertEqual(self.tctx.situation_name, "research_spike")
        self.assertTrue(self.tctx.required_slots)
        self.assertIn("market_landscape", self.tctx.required_slots)

    def test_sequential_composition_operates_on_the_already_narrowed_list(self):
        # the undocumented-footgun finding, now pinned: pass an ALREADY
        # completeness-narrowed phase list (as the real call site does,
        # sequentially) and confirm the situation filter's fallback returns
        # exactly THAT narrowed set, never reaching back into phases that
        # were already excluded upstream.
        sitlib.ensure_seeded(self.fleet_dir)
        narrowed = self.phases[:2]   # simulate an upstream completeness cut
        out = self.orch._apply_situation_ref(
            self.tctx, {"situation": "research_spike"}, narrowed)
        self.assertEqual(out, narrowed)
        for p in out:
            self.assertIn(p, narrowed)


if __name__ == "__main__":
    unittest.main()
