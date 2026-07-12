"""Schema + referential-integrity guard for the shipped workflows/*.json.

Locks in the standardized shape (every workflow and phase carries every field)
and catches drift: a bad phase key, a build_phase that points nowhere, a
malformed verify block, or an unknown target won't silently ship.
"""
import glob
import json
import os
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


if __name__ == "__main__":
    unittest.main()
