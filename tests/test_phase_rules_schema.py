"""Schema guard for the shipped phase_rules.json (mirrors
test_workflows_schema.py): locks in the shape so a bad GUI edit (e.g. `rules`
turned into a string) is caught here instead of silently mis-rendering via
phase_rules._bullets().
"""
import json
import os
import unittest

import phase_rules as pr

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHASE_LIST_FIELDS = ("rules", "required_output", "acceptance_checks")


class TestPhaseRulesSchema(unittest.TestCase):
    def _raw(self):
        with open(os.path.join(HERE, pr.RULES_FILENAME), encoding="utf-8") as fh:
            return json.load(fh)

    def test_top_level_shape(self):
        data = self._raw()
        self.assertIsInstance(data.get("global_app_rules"), list)
        self.assertIsInstance(data.get("phases"), dict)
        self.assertTrue(data["phases"], "shipped phase_rules.json has no phases")

    def test_every_phase_entry_has_list_fields(self):
        data = self._raw()
        for key, phase in data["phases"].items():
            self.assertIsInstance(phase, dict, "phase %r is not an object" % key)
            for field in PHASE_LIST_FIELDS:
                if field in phase:
                    self.assertIsInstance(phase[field], list,
                                         "phase %r field %r is not a list" % (key, field))
                    for item in phase[field]:
                        self.assertIsInstance(item, str,
                                             "phase %r field %r has a non-string entry"
                                             % (key, field))

    def test_loads_cleanly_through_load_rules(self):
        loaded = pr.load_rules(HERE)
        self.assertEqual(set(loaded["phases"]), set(self._raw()["phases"]))


if __name__ == "__main__":
    unittest.main()
