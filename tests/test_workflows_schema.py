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
                "doc_sections", "test_deliverable", "conversational"}
KNOWN_TARGETS = {"app", "app_spec", "answer", "research", "productionize",
                 "audit", "enroll", "library_mining"}


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
                if p.get("conversational"):
                    # V3 board 1.3: conversational phases may declare rounds 0
                    # (unlimited); the engine ignores the budget entirely.
                    self.assertGreaterEqual(p["rounds"], 0)
                else:
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


class TestPhasePathSafety(unittest.TestCase):
    """from_json is the path-safety chokepoint: key/folder/file feed
    os.path.join(app_dir, folder, file) engine-wide, and workflow JSON is
    GUI/hand-editable — a '../' or absolute value must never escape the
    session dir (backfill.py guards its own join; every other consumer
    relies on this normalization)."""

    _BASE = {"key": "k", "purpose": "p"}

    def test_traversal_folder_falls_back_to_key(self):
        for bad in ("../../..", "/etc", "a/b", "..", "~x", " k "):
            p = wf.Phase.from_json(dict(self._BASE, folder=bad))
            self.assertEqual(p.folder, "k", "folder=%r must not survive" % bad)

    def test_traversal_file_falls_back_to_key_md(self):
        for bad in ("../escape.md", "/tmp/x.md", "a/b.md", ".."):
            p = wf.Phase.from_json(dict(self._BASE, file=bad))
            self.assertEqual(p.file, "k.md", "file=%r must not survive" % bad)

    def test_unsafe_key_is_fatal(self):
        # key is the fallback for folder/file, so a bad key can't be
        # silently patched — the phase is rejected outright.
        with self.assertRaises(ValueError):
            wf.Phase.from_json({"key": "../evil", "purpose": "p"})

    def test_dot_folder_still_allowed(self):
        # "." is the in-place marker chat phases use (chat.md at the app
        # root) — harmless in a join and must keep working.
        p = wf.Phase.from_json(dict(self._BASE, folder="."))
        self.assertEqual(p.folder, ".")

    def test_ordinary_values_untouched(self):
        p = wf.Phase.from_json({"key": "tech_specs", "folder": "specs",
                                "file": "specs.md", "purpose": "p"})
        self.assertEqual((p.key, p.folder, p.file),
                         ("tech_specs", "specs", "specs.md"))


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

    def test_unrecognized_bool_string_falls_back_to_schema_default(self):
        # "off"/"0.0" are neither canonical-true nor canonical-false: the
        # field's schema default (False for every bool field) wins, instead of
        # bool()'s any-non-empty-string-is-True fallthrough.
        for field in ("writes", "reads_target", "checkpoint",
                      "structurally_required", "requires_verification"):
            for val in ("off", "0.0", "maybe"):
                d = dict(self._BASE, **{field: val})
                self.assertIs(getattr(wf.Phase.from_json(d), field), False,
                              "%s=%r should fall back to the default" % (field, val))

    def test_dict_roles_not_misread_as_key_list(self):
        # A dict is iterable (its keys), but {"product": True} is a malformed
        # edit, not a one-role list — reject it like a bare string.
        d = dict(self._BASE, roles={"product": True})
        self.assertEqual(wf.Phase.from_json(d).roles, [])
        d = dict(self._BASE, doc_sections={"architecture": 1})
        self.assertEqual(wf.Phase.from_json(d).doc_sections, [])

    def test_bare_string_verify_coerced_to_none(self):
        # "verify": "xcodebuild" (string instead of dict) used to load fine and
        # then crash the engine's spec.get("repair_iterations", ...) mid-phase.
        d = dict(self._BASE, verify="xcodebuild")
        self.assertIsNone(wf.Phase.from_json(d).verify)

    def test_dict_verify_passes_through_unchanged(self):
        spec = {"type": "xcodebuild", "repair_iterations": 3}
        self.assertEqual(wf.Phase.from_json(dict(self._BASE, verify=spec)).verify,
                         spec)

    def test_non_dict_verify_shapes_all_coerced_to_none(self):
        for bad in ("http", ["xcodebuild"], 1, True, {}):
            d = dict(self._BASE, verify=bad)
            self.assertIsNone(wf.Phase.from_json(d).verify,
                              "verify=%r should coerce to None" % (bad,))


if __name__ == "__main__":
    unittest.main()


class TestChatWorkflowSeeds(unittest.TestCase):
    """V3 board 1.3: the two shipped conversational chat workflows."""

    NAMES = ("chat_ideas", "chat_research")

    def test_seeds_load_with_expected_shape(self):
        for name in self.NAMES:
            w = wf.load_workflow(name)
            self.assertEqual(w.name, name)
            self.assertEqual(len(w.phases), 1, name)
            p = w.phases[0]
            self.assertTrue(p.conversational, name)
            self.assertEqual(p.rounds, 0, name)   # unlimited (and ignored)
            # Chats must never gain write capability by accident.
            self.assertFalse(p.writes, name)
            self.assertIsNone(p.verify, name)
            self.assertIsNone(w.build_phase, name)

    def test_seed_roles_exist_in_roles_registry(self):
        import roles as roleslib
        known = {r["id"] for r in roleslib.DEFAULT_ROLES}
        for name in self.NAMES:
            for rid in wf.load_workflow(name).phases[0].roles:
                self.assertIn(rid, known, "%s uses unknown role %r" % (name, rid))

    def test_seeds_listed(self):
        listed = wf.list_workflows()
        for name in self.NAMES:
            self.assertIn(name, listed)

    def test_missing_disk_copy_degrades_to_shipped_fallback_not_app_build(self):
        # An orch_dir with NO workflows/ JSON at all: load must come from the
        # shipped in-memory fallback (_load_shipped_fallbacks), never silently
        # become app_build.
        import tempfile
        empty = tempfile.mkdtemp()
        for name in self.NAMES:
            w = wf.load_workflow(name, orch_dir=empty)
            self.assertEqual(w.name, name)
            self.assertTrue(w.phases[0].conversational)
