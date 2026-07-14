"""write_tests phase + run_tests verify-spec wiring (item 2): opt-in only for
the deep pipelines (app_build/full_max), never for the speed-oriented ones."""
import glob
import json
import os
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEED_ORIENTED = ("sprint", "vslice", "prototype", "iterate")
DEEP_PIPELINES = ("app_build", "full_max")


def _load(name):
    with open(os.path.join(HERE, "workflows", "%s.json" % name), encoding="utf-8") as fh:
        return json.load(fh)


class TestWriteTestsPhasePlacement(unittest.TestCase):
    def test_deep_pipelines_have_write_tests_before_build_verification(self):
        for name in DEEP_PIPELINES:
            d = _load(name)
            keys = [p["key"] for p in d["phases"]]
            self.assertIn("write_tests", keys, name)
            self.assertLess(keys.index("write_tests"), keys.index("build_verification"), name)
            self.assertGreater(keys.index("write_tests"), keys.index("build_coordination"), name)

    def test_deep_pipelines_set_run_tests_on_build_verification(self):
        for name in DEEP_PIPELINES:
            d = _load(name)
            bv = next(p for p in d["phases"] if p["key"] == "build_verification")
            self.assertTrue(bv["verify"].get("run_tests"), name)

    def test_speed_oriented_workflows_have_no_write_tests_phase(self):
        for name in SPEED_ORIENTED:
            d = _load(name)
            keys = [p["key"] for p in d["phases"]]
            self.assertNotIn("write_tests", keys, name)

    def test_speed_oriented_workflows_never_set_run_tests(self):
        for name in SPEED_ORIENTED:
            d = _load(name)
            for p in d["phases"]:
                verify = p.get("verify") or {}
                self.assertFalse(verify.get("run_tests"),
                                 "%s phase %s sets run_tests" % (name, p["key"]))

    def test_no_other_workflow_sets_run_tests_either(self):
        # Only app_build/full_max should carry run_tests anywhere at all.
        for f in sorted(glob.glob(os.path.join(HERE, "workflows", "*.json"))):
            name = os.path.basename(f)[:-5]
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            for p in d["phases"]:
                verify = p.get("verify") or {}
                if verify.get("run_tests"):
                    self.assertIn(name, DEEP_PIPELINES,
                                 "%s unexpectedly sets run_tests" % name)

    def test_write_tests_has_a_test_deliverable_set(self):
        for name in DEEP_PIPELINES:
            d = _load(name)
            wt = next(p for p in d["phases"] if p["key"] == "write_tests")
            self.assertTrue(wt.get("test_deliverable"))

    def test_write_tests_covered_by_phase_rules(self):
        import phase_rules
        rules = phase_rules.load_rules(HERE)
        self.assertIn("write_tests", rules.get("phases", {}))
        entry = rules["phases"]["write_tests"]
        self.assertTrue(entry.get("rules"))
        self.assertTrue(entry.get("acceptance_checks"))


if __name__ == "__main__":
    unittest.main()
