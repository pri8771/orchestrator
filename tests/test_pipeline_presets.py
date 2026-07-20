"""V3 7.11: pipeline preset schema + validator (pure leaf).

A pipeline is a saved preset of Conductor routing rules + a goal manifest —
validate_preset reuses conductor_routing.validate_rule and
conductor_termination.normalize_manifest directly (one validation path, no
schema drift). Round-trip / GUI-side tests live in the Swift suite.
"""
import unittest

import pipeline_presets as pp


KNOWN = {"ideas", "research", "planning"}


def _preset(**over):
    base = {
        "preset_name": "Brainstorm to Plan",
        "routing": {"artifact_routes": {}, "rules": [
            {"match": {"artifact_type": "idea"}, "strategy": "one",
             "targets": ["research"]}]},
        "goal_manifest": {"goal": {"doc_gap_empty": True},
                          "quiescence_cycles": 3},
        "seed": {"section": "ideas", "prompt_template": "seed: {{idea}}"},
        "ui": {"nodes": [{"id": "ideas", "x": 10, "y": 20}]},
    }
    base.update(over)
    return base


class TestValidatePreset(unittest.TestCase):
    def test_valid_preset_normalizes_cleanly(self):
        norm, err = pp.validate_preset(_preset(), KNOWN)
        self.assertIsNone(err)
        self.assertEqual(norm["preset_name"], "Brainstorm to Plan")
        self.assertEqual(norm["routing"]["rules"][0]["targets"], ["research"])
        self.assertTrue(norm["goal_manifest"]["goal"]["doc_gap_empty"])
        self.assertEqual(norm["seed"]["section"], "ideas")

    def test_missing_preset_name(self):
        _, err = pp.validate_preset(_preset(preset_name=""), KNOWN)
        self.assertIn("preset_name", err)

    def test_rule_error_names_the_edge_index(self):
        bad = _preset(routing={"artifact_routes": {}, "rules": [
            {"match": {"artifact_type": "idea"}, "strategy": "bogus",
             "targets": ["research"]}]})
        _, err = pp.validate_preset(bad, KNOWN)
        self.assertIn("routing.rules[0]", err)
        self.assertIn("strategy", err)

    def test_route_target_must_be_a_known_section(self):
        bad = _preset(routing={"artifact_routes": {}, "rules": [
            {"match": {"artifact_type": "idea"}, "strategy": "one",
             "targets": ["nope"]}]})
        _, err = pp.validate_preset(bad, KNOWN)
        self.assertIn("unknown section", err)
        self.assertIn("nope", err)

    def test_invalid_goal_manifest_field_refuses_not_downgrades(self):
        # normalize_manifest would silently drop an invalid quiescence_cycles
        # to None (safe default for a NORMAL file) — a PRESET must refuse
        # explicitly instead, since the author asked for that exact behavior.
        bad = _preset(goal_manifest={"quiescence_cycles": -1})
        norm, err = pp.validate_preset(bad, KNOWN)
        self.assertIsNone(norm)
        self.assertIn("goal_manifest", err)

    def test_missing_seed_section(self):
        bad = _preset(seed={"section": "", "prompt_template": "x"})
        _, err = pp.validate_preset(bad, KNOWN)
        self.assertIn("seed.section", err)

    def test_seed_section_must_be_known(self):
        bad = _preset(seed={"section": "nope", "prompt_template": "x"})
        _, err = pp.validate_preset(bad, KNOWN)
        self.assertIn("seed.section", err)
        self.assertIn("nope", err)

    def test_unknown_top_level_ui_keys_survive_verbatim(self):
        preset = _preset(ui={"nodes": [{"id": "a"}], "future_field": 123})
        norm, err = pp.validate_preset(preset, KNOWN)
        self.assertIsNone(err)
        self.assertEqual(norm["ui"], {"nodes": [{"id": "a"}],
                                      "future_field": 123})

    def test_missing_ui_defaults_to_empty_object(self):
        preset = _preset()
        del preset["ui"]
        norm, err = pp.validate_preset(preset, KNOWN)
        self.assertIsNone(err)
        self.assertEqual(norm["ui"], {})

    def test_not_an_object(self):
        _, err = pp.validate_preset("nope", KNOWN)
        self.assertIn("preset", err)

    def test_as_route_config_matches_conductor_routing_shape(self):
        norm, _ = pp.validate_preset(_preset(), KNOWN)
        cfg = pp.as_route_config(norm)
        self.assertTrue(cfg.ok)
        self.assertEqual(cfg.rules[0]["targets"], ["research"])

    def test_edge_specific_source_section_survives_validation(self):
        # a canvas edge A->B (not "any producer of X routes to B") depends on
        # match.source_section surviving into the normalized rule — this is
        # the difference between an EDGE and a type-wide wildcard route.
        preset = _preset(routing={"artifact_routes": {}, "rules": [
            {"match": {"artifact_type": "idea", "source_section": "ideas"},
             "strategy": "one", "targets": ["research"]}]})
        norm, err = pp.validate_preset(preset, KNOWN)
        self.assertIsNone(err)
        self.assertEqual(norm["routing"]["rules"][0]["source_section"],
                         "ideas")
        cfg = pp.as_route_config(norm)
        # rules_for (conductor_routing.py) is what actually enforces this at
        # route-plan time; confirm the SAME config object reaches it intact.
        import conductor_routing as crlib
        matched = crlib.rules_for("idea", "ideas", cfg)
        self.assertEqual(len(matched), 1)
        not_matched = crlib.rules_for("idea", "planning", cfg)
        self.assertEqual(not_matched, [],
                         "a source_section-scoped edge must not fire from "
                         "an unrelated section")


class TestLoadPresetFile(unittest.TestCase):
    def test_missing_file_refuses(self):
        _, err = pp.load_preset_file("/nonexistent/path.json", KNOWN)
        self.assertIn("unreadable", err)

    def test_corrupt_json_refuses(self):
        import tempfile, os
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as fh:
            fh.write("{not json")
        try:
            _, err = pp.load_preset_file(path, KNOWN)
            self.assertIn("invalid JSON", err)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
