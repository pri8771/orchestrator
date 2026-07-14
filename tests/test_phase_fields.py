import json
import os
import shutil
import tempfile
import unittest
import workflows as wf


class TestPhaseFields(unittest.TestCase):
    def test_legacy_json_loads_with_defaults(self):
        p = wf.Phase.from_json({"key": "x", "purpose": "y"})
        self.assertFalse(p.checkpoint)
        self.assertFalse(p.structurally_required)
        self.assertFalse(p.requires_verification)
        self.assertEqual(p.doc_sections, [])
        self.assertIsNone(p.test_deliverable)

    def test_new_fields_roundtrip(self):
        p = wf.Phase("scope_and_prd", "f", "f.md", "purpose",
                     checkpoint=True, structurally_required=True,
                     requires_verification=True, doc_sections=["prd.summary"],
                     test_deliverable="xcode")
        d = p.to_json()
        p2 = wf.Phase.from_json(d)
        self.assertTrue(p2.checkpoint)
        self.assertTrue(p2.structurally_required)
        self.assertTrue(p2.requires_verification)
        self.assertEqual(p2.doc_sections, ["prd.summary"])
        self.assertEqual(p2.test_deliverable, "xcode")

    def test_legacy_tuple_compat_preserved(self):
        p = wf.Phase("k", "fold", "file.md", "purpose")
        key, folder, file, purpose = p  # unpacks as 4-tuple
        self.assertEqual((key, folder, file, purpose), ("k", "fold", "file.md", "purpose"))
        self.assertEqual(p[0], "k")

    def test_builtin_workflows_still_load(self):
        for name in ("app_build", "audit", "sprint", "productionize", "research", "answer_question"):
            w = wf.load_workflow(name)
            self.assertTrue(w.phases, "%s has no phases" % name)


class TestLoadWorkflowMalformedJson(unittest.TestCase):
    """load_workflow never raises on user-edited workflows/*.json — it falls
    back to the built-in. TypeError shapes (null rounds, non-dict budget,
    non-iterable roles) used to crash the run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "workflows"))

    def _write(self, payload):
        with open(os.path.join(self.tmp, "workflows", "app_build.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_null_rounds_falls_back_to_builtin(self):
        self._write({"name": "app_build", "phases": [
            {"key": "x", "purpose": "y", "rounds": None}]})
        w = wf.load_workflow("app_build", self.tmp)
        self.assertTrue(w.phases)
        self.assertIsNone(w.phase("x"))  # built-in, not the broken file

    def test_non_iterable_roles_coerces_to_empty_list(self):
        # roles=5 used to TypeError inside list() and reject the whole file;
        # _as_str_list now coerces every non-list/tuple shape (string, dict,
        # int) to [] uniformly, so the edited file still loads.
        self._write({"name": "app_build", "phases": [
            {"key": "x", "purpose": "y", "roles": 5}]})
        w = wf.load_workflow("app_build", self.tmp)
        self.assertTrue(w.phases)
        self.assertEqual(w.phase("x").roles, [])

    def test_non_dict_budget_falls_back_to_builtin(self):
        self._write({"name": "app_build", "budget": 3, "phases": [
            {"key": "x", "purpose": "y"}]})
        w = wf.load_workflow("app_build", self.tmp)
        self.assertTrue(w.phases)
        self.assertIsNone(w.phase("x"))


if __name__ == "__main__":
    unittest.main()
