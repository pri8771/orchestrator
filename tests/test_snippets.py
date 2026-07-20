import json
import os
import shutil
import tempfile
import unittest

import snippets


class TestSnippetLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snippets-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.orch = os.path.join(self.tmp, "orch")
        self.project = os.path.join(self.tmp, "project")

    def _write(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_shipped_file_matches_defaults(self):
        with open(snippets.fleet_path(snippets.HERE), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), snippets.DEFAULT_SNIPPETS)
        self.assertEqual(len(snippets.DEFAULT_SNIPPETS), 8)

    def test_seed_only_when_absent(self):
        path = snippets.ensure_seeded(self.orch)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(set(snippets.load_snippets(self.orch)),
                         {row["name"] for row in snippets.DEFAULT_SNIPPETS})
        custom = [{"name": "mine", "phase": "", "text": "keep"}]
        self._write(path, custom)
        snippets.ensure_seeded(self.orch)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), custom)

    def test_project_shadows_section_shadows_fleet_and_unshadows(self):
        fleet = snippets.fleet_path(self.orch)
        section = snippets.section_path(self.orch, "ideas")
        project = snippets.project_path(self.project)
        self._write(fleet, [{"name": "simplify", "text": "fleet"}])
        self._write(section, [{"name": "simplify", "text": "section"}])
        self._write(project, [{"name": "simplify", "text": "project"}])
        loaded = snippets.load_snippets(self.orch, "ideas", self.project)
        self.assertEqual(loaded["simplify"]["text"], "project")
        os.remove(project)
        loaded = snippets.load_snippets(self.orch, "ideas", self.project)
        self.assertEqual(loaded["simplify"]["text"], "section")
        os.remove(section)
        loaded = snippets.load_snippets(self.orch, "ideas", self.project)
        self.assertEqual(loaded["simplify"]["text"], "fleet")

    def test_corrupt_layer_warns_and_other_layers_survive(self):
        self._write(snippets.fleet_path(self.orch),
                    [{"name": "safe", "text": "fleet"}])
        path = snippets.section_path(self.orch, "ideas")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{bad")
        warned = []
        loaded = snippets.load_snippets(self.orch, "ideas", self.project,
                                        warned.append)
        self.assertIn("safe", loaded)
        self.assertTrue(any("section" in warning and "skipped" in warning
                            for warning in warned))

    def test_malformed_variables_degrade_to_plain_with_warning(self):
        path = snippets.fleet_path(self.orch)
        self._write(path, [{"name": "broken", "text": "Keep {{x}}",
                            "variables": {"name": "x"}},
                           {"name": "old", "phase": "", "text": "plain"}])
        warned = []
        loaded = snippets.load_snippets(self.orch, on_warn=warned.append)
        self.assertEqual(loaded["broken"],
                         {"name": "broken", "phase": "", "text": "Keep {{x}}"})
        self.assertEqual(loaded["old"],
                         {"name": "old", "phase": "", "text": "plain"})
        self.assertTrue(any("plain text" in warning for warning in warned))

    def test_typed_variables_round_trip(self):
        value = [{"name": "typed", "phase": "chat", "text": "{{tone}}",
                  "variables": [{"name": "tone", "label": "Tone",
                                 "type": "choice", "options": ["brief", "deep"],
                                 "required": True, "default": "brief"}]}]
        path = os.path.join(self.tmp, "saved", "snippets.json")
        snippets.save_snippets(value, path)
        loaded = snippets._load_layer(path, "test")
        self.assertEqual(loaded["typed"], value[0])


if __name__ == "__main__":
    unittest.main()
