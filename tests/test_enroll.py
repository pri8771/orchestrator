"""External-codebase enrollment intake (E1)."""

import os
import subprocess
import sys
import tempfile
import unittest

import enroll


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestEnrollScaffold(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.workspace = os.path.join(self.base, "workspace")
        self.origin = os.path.join(self.base, "Existing App")
        os.makedirs(self.origin)
        _write(os.path.join(self.origin, "README.md"),
               "Observed title\n" + "line\n" * 60)
        _write(os.path.join(self.origin, "Package.swift"), "// swift-tools-version: 5.9\n")
        _write(os.path.join(self.origin, "Sources", "Feature.swift"),
               "struct Feature {}\nlet answer = 42\n")

    def test_scaffold_has_read_only_target_contract_and_observed_prompt(self):
        result = enroll.scaffold(self.workspace, self.origin)
        self.assertEqual(result["slug"], "existing-app")
        app = os.path.join(self.workspace, "existing-app")
        with open(os.path.join(app, "target_path.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), os.path.realpath(self.origin) + "\n")
        with open(os.path.join(app, "workflow.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "enroll\n")
        with open(os.path.join(app, "initial_prompt", "initial_prompt.md"),
                  encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertIn("Swift package (observed marker: `Package.swift`)", prompt)
        self.assertIn("[FROM-THEIR-DOCS: README.md]", prompt)
        self.assertEqual(prompt.count("> line"), 49)  # title + 49 lines = 50
        self.assertIn("`.swift`: 3 line(s) across 2 file(s)", prompt)
        self.assertIn("`Sources` — directory", prompt)

    def test_missing_readme_is_stated_not_invented(self):
        os.remove(os.path.join(self.origin, "README.md"))
        result = enroll.scaffold(self.workspace, self.origin)
        with open(os.path.join(result["app_dir"], "initial_prompt",
                               "initial_prompt.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertIn("No top-level README file was observed.", prompt)
        self.assertNotIn("[FROM-THEIR-DOCS:", prompt)

    def test_slug_collision_refuses_without_suffix_or_mutation(self):
        first = enroll.scaffold(self.workspace, self.origin, name="adopted")
        sentinel = os.path.join(first["app_dir"], "sentinel")
        _write(sentinel, "keep")
        with self.assertRaisesRegex(enroll.EnrollError, "already exists"):
            enroll.scaffold(self.workspace, self.origin, name="adopted")
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "adopted-2")))
        with open(sentinel, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep")

    def test_origin_inside_workspace_is_rejected_before_any_project_write(self):
        nested = os.path.join(self.workspace, "external")
        os.makedirs(nested)
        before = set(os.listdir(self.workspace))
        with self.assertRaisesRegex(enroll.EnrollError, "disjoint"):
            enroll.scaffold(self.workspace, nested, name="bad")
        self.assertEqual(set(os.listdir(self.workspace)), before)

    def test_workspace_inside_origin_is_also_rejected(self):
        workspace = os.path.join(self.origin, "orchestrator-workspace")
        with self.assertRaisesRegex(enroll.EnrollError, "disjoint"):
            enroll.scaffold(workspace, self.origin, name="bad")
        self.assertFalse(os.path.exists(workspace))

    def test_non_git_origin_warns_but_succeeds(self):
        result = enroll.scaffold(self.workspace, self.origin)
        self.assertTrue(os.path.isdir(result["app_dir"]))
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("not a Git repository root", result["warnings"][0])


class TestEnrollCLI(unittest.TestCase):
    def test_cli_scaffolds_and_prints_warning(self):
        base = tempfile.mkdtemp()
        origin = os.path.join(base, "source")
        workspace = os.path.join(base, "workspace")
        os.makedirs(origin)
        _write(os.path.join(origin, "main.py"), "print('observed')\n")
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"),
             "--root", workspace, "--enroll", origin, "--name", "taken-in"],
            cwd=HERE, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("WARN --enroll: target is not a Git repository root", proc.stdout)
        self.assertIn("ENROLLED: taken-in", proc.stdout)
        self.assertTrue(os.path.isfile(os.path.join(
            workspace, "taken-in", "initial_prompt", "initial_prompt.md")))

    def test_name_without_enroll_is_rejected(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "orchestrator.py"), "--name", "x"],
            cwd=HERE, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--name requires --enroll", proc.stderr)


if __name__ == "__main__":
    unittest.main()
