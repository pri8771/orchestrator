"""Tests for miniyaml.py — the extracted config.yaml reader."""
import os
import unittest

import miniyaml
import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestCoerceScalar(unittest.TestCase):
    def test_bools_ints_floats_null(self):
        self.assertIs(miniyaml.coerce_scalar("true"), True)
        self.assertIs(miniyaml.coerce_scalar("false"), False)
        self.assertEqual(miniyaml.coerce_scalar("42"), 42)
        self.assertEqual(miniyaml.coerce_scalar("3.5"), 3.5)
        self.assertIsNone(miniyaml.coerce_scalar("null"))
        self.assertIsNone(miniyaml.coerce_scalar("~"))

    def test_quoted_strings_unwrapped(self):
        self.assertEqual(miniyaml.coerce_scalar('"hi"'), "hi")
        self.assertEqual(miniyaml.coerce_scalar("'hi'"), "hi")

    def test_unterminated_quote_leading_stripped(self):
        # An unmatched leading quote is dropped, not kept as a corrupting prefix.
        self.assertEqual(miniyaml.coerce_scalar('"oops'), "oops")
        self.assertEqual(miniyaml.coerce_scalar('"123'), 123)


class TestStripInlineComment(unittest.TestCase):
    def test_strips_unquoted(self):
        self.assertEqual(miniyaml.strip_inline_comment("value # note"), "value")

    def test_keeps_hash_in_quotes(self):
        self.assertEqual(miniyaml.strip_inline_comment('"#007AFF"'), '"#007AFF"')

    def test_keeps_hash_without_space(self):
        self.assertEqual(miniyaml.strip_inline_comment("http://x#y"), "http://x#y")

    def test_comment_only_is_empty(self):
        self.assertEqual(miniyaml.strip_inline_comment("# all comment"), "")


class TestParseMinYaml(unittest.TestCase):
    def test_nested_maps_and_scalars(self):
        got = miniyaml.parse_min_yaml(
            "models:\n  claude: \"c\"  # note\n  n: 5\nruntime:\n  flag: true\n")
        self.assertEqual(got, {"models": {"claude": "c", "n": 5},
                               "runtime": {"flag": True}})

    def test_reexported_from_orchestrator(self):
        # Backward-compat: orchestrator still exposes the same callables.
        self.assertIs(orch.parse_min_yaml, miniyaml.parse_min_yaml)


class TestConfigYamlScalarShapes(unittest.TestCase):
    """Round-trip every scalar shape actually used in config.yaml (not just the
    synthetic examples above), so a shape that's only ever appeared in the real
    file — a quoted string with parens/dots, an empty quoted string, a quoted
    comma-list, a nested int map — has a pinned regression test."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
            cls.cfg = miniyaml.parse_min_yaml(fh.read())

    def test_quoted_string_with_punctuation(self):
        # root: "~/Documents/iOS-App-Factory" and models.gemini: "Gemini 3.5
        # Flash (High)" — quotes, slashes, dots, parens, spaces.
        self.assertEqual(self.cfg["root"], "~/Documents/iOS-App-Factory")
        self.assertEqual(self.cfg["models"]["gemini"], "Gemini 3.5 Flash (High)")

    def test_empty_quoted_string(self):
        self.assertEqual(self.cfg["models"]["gemini_api_key_file"], "")
        self.assertEqual(self.cfg["models"]["claude_reasoning"], "")

    def test_quoted_comma_separated_list_string(self):
        # Stays one string; list-splitting is the caller's job, not miniyaml's.
        roster = self.cfg["models"]["ollama_roster"]
        self.assertIsInstance(roster, str)
        self.assertIn(",", roster)
        self.assertIn("qwen2.5-coder:14b", roster)

    def test_bare_word_string(self):
        self.assertEqual(self.cfg["models"]["ollama"], "glm4:9b")
        self.assertEqual(self.cfg["ios"]["code_sign_style"], "Automatic")

    def test_bools(self):
        self.assertIs(self.cfg["agents"]["codex_enabled"], True)
        self.assertIs(self.cfg["runtime"]["worktree_isolation"], False)

    def test_ints(self):
        self.assertEqual(self.cfg["models"]["local_active_limit"], 3)
        self.assertEqual(self.cfg["runtime"]["timeout_seconds_per_agent"], 1800)
        self.assertIsInstance(self.cfg["runtime"]["timeout_seconds_per_agent"], int)

    def test_nested_map_of_ints(self):
        self.assertEqual(self.cfg["runtime"]["global_worker_cap"],
                         {"cli_remote": 12, "local_model": 1})

    def test_top_level_rounds_map(self):
        # A whole nested map whose every value is the same int scalar shape.
        self.assertEqual(self.cfg["rounds"]["prompt_contract"], 9)
        self.assertTrue(all(isinstance(v, int) for v in self.cfg["rounds"].values()))

    def test_loads_via_orchestrator_load_config(self):
        # End-to-end: the real loader orchestrator.py uses at startup.
        loaded = orch.load_config()
        self.assertEqual(loaded, self.cfg)


if __name__ == "__main__":
    unittest.main()
