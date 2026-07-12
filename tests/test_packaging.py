"""Guard pyproject.toml's [tool.setuptools] py-modules against drift: every
root-level engine module must be listed, or `pip install .` ships an engine
that's silently missing a module (seed_demo/simulate_stream were, until this
was added).
"""
import glob
import os
import unittest

try:
    import tomllib
except ImportError:  # Python 3.9/3.10 — the test is skipped there, not broken
    tomllib = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Root .py files that are NOT part of the installable engine package.
_NON_PACKAGE_MODULES = set()


@unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+")
class TestPyModulesMatchRootFiles(unittest.TestCase):
    def test_every_root_module_is_listed(self):
        with open(os.path.join(HERE, "pyproject.toml"), "rb") as fh:
            data = tomllib.load(fh)
        listed = set(data["tool"]["setuptools"]["py-modules"])
        on_disk = {os.path.basename(f)[:-3]
                  for f in glob.glob(os.path.join(HERE, "*.py"))} - _NON_PACKAGE_MODULES
        missing = on_disk - listed
        self.assertEqual(missing, set(),
                         "root modules missing from py-modules: %s" % sorted(missing))

    def test_no_stale_entries(self):
        with open(os.path.join(HERE, "pyproject.toml"), "rb") as fh:
            data = tomllib.load(fh)
        listed = set(data["tool"]["setuptools"]["py-modules"])
        on_disk = {os.path.basename(f)[:-3] for f in glob.glob(os.path.join(HERE, "*.py"))}
        stale = listed - on_disk
        self.assertEqual(stale, set(),
                         "py-modules entries with no matching file: %s" % sorted(stale))


if __name__ == "__main__":
    unittest.main()
