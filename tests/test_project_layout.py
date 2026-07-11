import os
import tempfile
import unittest

import orchestrator as orch


def _mk_project(root, name):
    prompt_dir = os.path.join(root, name, "initial_prompt")
    os.makedirs(prompt_dir, exist_ok=True)
    with open(os.path.join(prompt_dir, "initial_prompt.md"), "w", encoding="utf-8") as fh:
        fh.write("Build something.\n")


class TestProjectLayout(unittest.TestCase):
    def test_workspace_projects_are_direct_children_only(self):
        root = tempfile.mkdtemp()
        _mk_project(root, "first-app")
        _mk_project(root, "second-app")

        # This nested project shape is intentionally ignored. Five requested
        # projects should be five sibling folders under root, not one wrapper
        # folder containing five children.
        _mk_project(os.path.join(root, "batch-wrapper"), "nested-app")

        os.makedirs(os.path.join(root, "notes"), exist_ok=True)
        self.assertEqual(orch.find_apps(root), ["first-app", "second-app"])


if __name__ == "__main__":
    unittest.main()
