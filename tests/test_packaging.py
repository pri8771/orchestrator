"""Guard pyproject.toml's [tool.setuptools] py-modules against drift: every
root-level engine module must be listed, or `pip install .` ships an engine
that's silently missing a module (seed_demo/simulate_stream were, until this
was added).

No `tomllib` (stdlib since 3.11) and no `tomli` (would violate this project's
stdlib-only rule) — Python 3.9/3.10 CI legs need this test to run too. Instead
of a real TOML parser, this file carries a narrow, hand-rolled extractor for
exactly the one shape pyproject.toml actually uses for py-modules: a
`py-modules = [` line followed by comma-separated double-quoted strings up to
a closing `]`, no nesting, no inline comments. It is deliberately NOT a
general TOML parser — see _extract_py_modules's docstring for why it raises
loudly instead of guessing when it meets syntax it doesn't recognize.
"""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Root .py files that are NOT part of the installable engine package.
_NON_PACKAGE_MODULES = set()

_ARRAY_START_RE = re.compile(r"^py-modules\s*=\s*\[", re.MULTILINE)


def _extract_py_modules(pyproject_text):
    """Return the list of strings in pyproject.toml's `py-modules = [...]`
    array. Handles exactly this file's real syntax (plain double-quoted
    strings, comma-separated, optionally spread across multiple lines, with
    an optional trailing comma) and nothing more. Any other shape — a nested
    bracket, a single-quoted or non-string entry, a `#` inside the array —
    raises ValueError rather than silently returning a wrong/partial list, so
    a future syntax change fails this test loudly instead of passing on a
    stale/mis-parsed set of names."""
    m = _ARRAY_START_RE.search(pyproject_text)
    if not m:
        raise ValueError("py-modules = [ ... ] not found in pyproject.toml")
    start = m.end()
    end = pyproject_text.find("]", start)
    if end == -1:
        raise ValueError("py-modules array has no closing ']'")
    body = pyproject_text[start:end]
    if "[" in body or "#" in body:
        raise ValueError("py-modules array contains unexpected '[' or '#' — "
                         "syntax this narrow parser doesn't understand: %r" % body)
    modules = []
    for raw_entry in body.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue   # trailing comma / blank line between entries
        if not (len(entry) >= 2 and entry[0] == '"' and entry[-1] == '"'):
            raise ValueError("py-modules entry isn't a plain double-quoted "
                             "string: %r" % entry)
        inner = entry[1:-1]
        if '"' in inner or "'" in inner:
            raise ValueError("py-modules entry has unexpected quoting: %r" % entry)
        modules.append(inner)
    return modules


class TestExtractPyModules(unittest.TestCase):
    """The hand-rolled extractor itself, against small synthetic inputs —
    separate from the real-file guard tests below."""

    def test_single_line(self):
        self.assertEqual(_extract_py_modules('py-modules = ["a", "b", "c"]'),
                         ["a", "b", "c"])

    def test_multi_line_with_trailing_comma(self):
        text = 'py-modules = [\n    "a", "b",\n    "c",\n]\n'
        self.assertEqual(_extract_py_modules(text), ["a", "b", "c"])

    def test_missing_array_raises(self):
        with self.assertRaises(ValueError):
            _extract_py_modules("[tool.setuptools]\n")

    def test_unclosed_array_raises(self):
        with self.assertRaises(ValueError):
            _extract_py_modules('py-modules = [\n    "a", "b"\n')

    def test_non_string_entry_raises(self):
        with self.assertRaises(ValueError):
            _extract_py_modules("py-modules = [a, b]")

    def test_inline_comment_raises_rather_than_mis_parses(self):
        with self.assertRaises(ValueError):
            _extract_py_modules('py-modules = [\n    "a",  # comment\n    "b",\n]\n')


class TestPyModulesMatchRootFiles(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8") as fh:
            self.listed = set(_extract_py_modules(fh.read()))

    def test_every_root_module_is_listed(self):
        on_disk = {os.path.basename(f)[:-3]
                  for f in glob.glob(os.path.join(HERE, "*.py"))} - _NON_PACKAGE_MODULES
        missing = on_disk - self.listed
        self.assertEqual(missing, set(),
                         "root modules missing from py-modules: %s" % sorted(missing))

    def test_no_stale_entries(self):
        on_disk = {os.path.basename(f)[:-3] for f in glob.glob(os.path.join(HERE, "*.py"))}
        stale = self.listed - on_disk
        self.assertEqual(stale, set(),
                         "py-modules entries with no matching file: %s" % sorted(stale))


if __name__ == "__main__":
    unittest.main()
