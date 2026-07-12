"""Tests for miniyaml.py — the extracted config.yaml reader."""
import unittest

import miniyaml
import orchestrator as orch


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

    def test_unterminated_quote_returned_verbatim(self):
        self.assertEqual(miniyaml.coerce_scalar('"oops'), '"oops')


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


if __name__ == "__main__":
    unittest.main()
