"""Schema + referential-integrity guard for the shipped workflows/*.json.

Locks in the standardized shape (every workflow and phase carries every field)
and catches drift: a bad phase key, a build_phase that points nowhere, a
malformed verify block, or an unknown target won't silently ship.
"""
import glob
import json
import os
import tempfile
import unittest

import workflows as wf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_FIELDS = {"name", "title", "description", "target", "build_phase",
             "budget", "overrides", "phases"}
PHASE_FIELDS = {"key", "folder", "file", "title", "purpose", "rounds", "roles",
                "writes", "reads_target", "verify", "checkpoint",
                "structurally_required", "requires_verification",
                "doc_sections", "test_deliverable"}
KNOWN_TARGETS = {"app", "app_spec", "answer", "research", "productionize",
                 "audit", "library_mining"}


def _load(f):
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)


def _files():
    return sorted(glob.glob(os.path.join(HERE, "workflows", "*.json")))


class TestWorkflowsSchema(unittest.TestCase):
    def test_at_least_the_documented_count(self):
        self.assertGreaterEqual(len(_files()), 14)

    def test_every_file_has_uniform_fields(self):
        for f in _files():
            d = _load(f)
            self.assertEqual(set(d), WF_FIELDS, os.path.basename(f))
            self.assertTrue(d["phases"], "%s has no phases" % f)
            for p in d["phases"]:
                self.assertEqual(set(p), PHASE_FIELDS,
                                 "%s phase %s" % (os.path.basename(f), p.get("key")))

    def test_field_types(self):
        for f in _files():
            d = _load(f)
            self.assertIn(d["target"], KNOWN_TARGETS, os.path.basename(f))
            self.assertTrue(isinstance(d["budget"], (dict, type(None))))
            self.assertTrue(isinstance(d["overrides"], (dict, type(None))))
            for p in d["phases"]:
                self.assertIsInstance(p["rounds"], int)
                self.assertGreater(p["rounds"], 0)
                self.assertIsInstance(p["roles"], list)
                self.assertIsInstance(p["writes"], bool)
                self.assertTrue(isinstance(p["verify"], (dict, type(None))))
                if isinstance(p["verify"], dict):
                    self.assertIn("type", p["verify"])

    def test_referential_integrity(self):
        for f in _files():
            d = _load(f)
            keys = [p["key"] for p in d["phases"]]
            self.assertEqual(len(keys), len(set(keys)),
                             "%s has duplicate phase keys" % os.path.basename(f))
            if d["build_phase"] is not None:
                self.assertIn(d["build_phase"], keys,
                              "%s build_phase not a real phase" % os.path.basename(f))

    def test_loads_via_workflow_model(self):
        # Every file must load through the engine's own loader without raising
        # and preserve its phase count.
        for f in _files():
            d = _load(f)
            w = wf.Workflow.from_json(d)
            self.assertEqual(len(w.phases), len(d["phases"]))
            self.assertEqual(w.name, d["name"])

    def test_every_shipped_workflow_has_an_in_memory_fallback(self):
        # Every workflows/*.json name must have a _BUILTINS entry — otherwise a
        # missing/corrupt on-disk file for that name silently substitutes
        # app_build instead of the correct workflow (e.g. `iterate` silently
        # becoming a full rebuild instead of a surgical change).
        names = {os.path.basename(f)[:-5] for f in _files()}
        self.assertEqual(names - set(wf._BUILTINS), set(),
                         "workflows with no _BUILTINS fallback: %s"
                         % sorted(names - set(wf._BUILTINS)))

    def test_corrupt_json_falls_back_to_the_correct_workflow_not_app_build(self):
        with tempfile.TemporaryDirectory() as d:
            wf.ensure_seeded(d)
            path = os.path.join(d, "workflows", "iterate.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            loaded = wf.load_workflow("iterate", d)
        self.assertEqual(loaded.name, "iterate")

    def test_missing_json_falls_back_to_the_correct_workflow_not_app_build(self):
        with tempfile.TemporaryDirectory() as d:
            wf.ensure_seeded(d)
            os.remove(os.path.join(d, "workflows", "vslice.json"))
            loaded = wf.load_workflow("vslice", d)
        self.assertEqual(loaded.name, "vslice")

    def test_ensure_seeded_recreates_every_shipped_workflow(self):
        with tempfile.TemporaryDirectory() as d:
            wf.ensure_seeded(d)
            seeded = {f[:-5] for f in os.listdir(os.path.join(d, "workflows"))
                     if f.endswith(".json")}
        self.assertEqual(seeded, set(wf._BUILTINS))

    def test_phase_rules_cover_every_phase(self):
        # Every phase key used by any workflow has a quality playbook entry (or
        # the playbook injection silently no-ops for that phase).
        import phase_rules
        rules = phase_rules.load_rules(HERE)
        covered = set(rules.get("phases", {}))
        used = set()
        for f in _files():
            for p in _load(f)["phases"]:
                used.add(p["key"])
        self.assertEqual(used - covered, set(),
                         "phases with no phase_rules entry: %s" % sorted(used - covered))


class TestPhaseFieldCoercion(unittest.TestCase):
    """bool()/list() applied directly to a raw JSON value misparses the
    stringly-typed values agents (and hand-edited JSON) sometimes emit."""

    _BASE = {"key": "k", "folder": "k", "file": "k.md", "purpose": "p"}

    def test_string_false_is_falsy(self):
        d = dict(self._BASE, writes="false")
        self.assertIs(wf.Phase.from_json(d).writes, False)

    def test_string_false_variants_are_falsy_for_every_bool_field(self):
        for field in ("writes", "reads_target", "checkpoint",
                     "structurally_required", "requires_verification"):
            for val in ("false", "False", "no", "0", ""):
                d = dict(self._BASE, **{field: val})
                self.assertIs(getattr(wf.Phase.from_json(d), field), False,
                             "%s=%r should be falsy" % (field, val))

    def test_string_true_is_still_truthy(self):
        d = dict(self._BASE, writes="true")
        self.assertIs(wf.Phase.from_json(d).writes, True)

    def test_actual_bool_still_works(self):
        d = dict(self._BASE, writes=True, checkpoint=False)
        self.assertIs(wf.Phase.from_json(d).writes, True)
        self.assertIs(wf.Phase.from_json(d).checkpoint, False)

    def test_bare_string_roles_not_shattered_into_characters(self):
        d = dict(self._BASE, roles="product")
        self.assertEqual(wf.Phase.from_json(d).roles, [])

    def test_bare_string_doc_sections_not_shattered_into_characters(self):
        d = dict(self._BASE, doc_sections="architecture")
        self.assertEqual(wf.Phase.from_json(d).doc_sections, [])

    def test_roles_list_still_works(self):
        d = dict(self._BASE, roles=["product", "eng"])
        self.assertEqual(wf.Phase.from_json(d).roles, ["product", "eng"])


if __name__ == "__main__":
    unittest.main()
