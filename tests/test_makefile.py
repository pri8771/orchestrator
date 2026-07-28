"""Hermetic test for the Makefile's `clean` target (A-42).

`pip install .` (pip >= 21.3 in-tree builds — CI's typecheck job does exactly
this) leaves a root-level build/ dir holding a stale full copy of every engine
module; clean must remove it, while the TRACKED sections/build/ path (a
different literal path) must survive. Running `make clean` inside the real
checkout from a test would delete the developer's gui/.build cache, so the
Makefile is copied into a scratch tree populated with fixture dirs and make
runs there — the clean recipe is self-contained (no $(PYTHON), no repo state).
"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
MAKEFILE = os.path.join(REPO_ROOT, "Makefile")


@unittest.skipUnless(shutil.which("make"), "requires make")
class TestMakeClean(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix="orch_makefile_")
        self.addCleanup(shutil.rmtree, self.tree, True)
        shutil.copy(MAKEFILE, os.path.join(self.tree, "Makefile"))

    def _mkfile(self, *rel):
        path = os.path.join(self.tree, *rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x\n")
        return path

    def _clean(self):
        return subprocess.run(["make", "clean"], cwd=self.tree,
                              capture_output=True, text=True, timeout=60)

    def test_clean_removes_root_build_dir(self):
        # The A-42 gap: the stale pip in-tree build copy at ./build.
        self._mkfile("build", "lib", "orchestrator.py")
        proc = self._clean()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.tree, "build")))

    def test_clean_preserves_tracked_sections_build(self):
        # rm targets the literal root-level ./build only — the committed
        # sections/build/ registry is a different path and must survive.
        kept = self._mkfile("sections", "build", "target_policy.json")
        self._mkfile("build", "lib", "orchestrator.py")
        self.assertEqual(self._clean().returncode, 0)
        self.assertTrue(os.path.isfile(kept))

    def test_clean_removes_the_other_build_artifacts(self):
        doomed = [
            self._mkfile("gui", ".build", "marker"),
            self._mkfile("gui", "dist", "Orchestrator.app"),
            self._mkfile("orchestrator.egg-info", "PKG-INFO"),
            self._mkfile("dist", "orchestrator-1.0.tar.gz"),
            self._mkfile(".pytest_cache", "CACHEDIR.TAG"),
            self._mkfile("tests", "__pycache__", "mod.cpython-312.pyc"),
        ]
        self.assertEqual(self._clean().returncode, 0)
        for path in doomed:
            self.assertFalse(os.path.exists(path), "%s survived clean" % path)


if __name__ == "__main__":
    unittest.main()
