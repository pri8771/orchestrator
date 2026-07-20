"""V3 board 8.3: the remaining five sections are runnable data."""
import json
import os
import shutil
import tempfile
import unittest

import artifacts as artlib
import events as evlib
import orchestrator as orch
import roles as roleslib
import sections as seclib


HERE = orch.HERE
NAMES = ("design", "gtm", "legal", "execution", "library")
EXPECTED_CAPABILITIES = {
    "design": {"writes": "workspace", "exec": False, "external": False},
    "gtm": {"writes": "workspace", "exec": False, "external": False},
    "legal": {"writes": "workspace", "exec": False, "external": False},
    "execution": {"writes": "workspace", "exec": False, "external": False},
    "library": {"writes": "workspace", "exec": True, "external": False},
}
PUBLISH_BLOCKS = {
    "design": {"type": "design_brief", "title": "Design direction"},
    "gtm": {"type": "positioning_brief", "title": "Positioning"},
    "legal": {"type": "legal_flag", "title": "Consent blocker",
              "blocking_concern": "Consent evidence is absent"},
    "execution": {"type": "release_checklist", "title": "Release checks"},
    "library": {"type": "knowledge_hint", "title": "Shared retry seam",
                "evidence": ["app-a/net.py", "app-b/net.py"]},
}


class TestRemainingSectionSeeds(unittest.TestCase):
    def test_manifests_load_without_fallback_and_capabilities_are_honest(self):
        app_dir = tempfile.mkdtemp(prefix="sections-8.3-load-")
        self.addCleanup(shutil.rmtree, app_dir, True)
        for name in NAMES:
            section = seclib.load_section(name, HERE, app_dir=app_dir)
            self.assertEqual(section.id, name)
            self.assertTrue(section.workflow.phases, name)
            self.assertEqual(section.capabilities,
                             EXPECTED_CAPABILITIES[name], name)
        self.assertEqual(evlib.read_events(
            app_dir, kinds=["config_fallback"]), [])

    def test_lint_has_no_errors_and_only_library_has_expected_exec_warning(self):
        for name in NAMES:
            report = seclib.lint_section(name, HERE)
            self.assertEqual([e for e in report
                              if e["severity"] == "error"], [], name)
            warnings = [e for e in report if e["severity"] == "warning"]
            if name == "library":
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("exec", warnings[0]["message"])
            else:
                self.assertEqual(warnings, [], name)

    def test_emitted_types_and_legal_review_gate_match_registry(self):
        emitted = set()
        for name in NAMES:
            emitted.update(seclib.load_section(
                name, HERE).artifact_types_emitted)
        self.assertTrue(emitted <= set(artlib.SEED_TYPES),
                        sorted(emitted - set(artlib.SEED_TYPES)))
        self.assertEqual(
            artlib.SEED_TYPES["legal_flag"]["finalization"],
            "requires_review_gate")

    def test_new_phase_keys_do_not_collide_with_each_other(self):
        owners = {}
        for name in NAMES:
            for phase in seclib.load_section(name, HERE).workflow.phases:
                owners.setdefault(phase.key, []).append(name)
        self.assertEqual({key: value for key, value in owners.items()
                          if len(value) > 1}, {})

    def test_section_personas_and_sampling_presets_resolve_without_warning(self):
        for name in NAMES:
            warnings = []
            section_dir = os.path.join(HERE, "sections", name)
            library = roleslib.load_agent_library(
                HERE, section_dir, on_warn=warnings.append)
            section_path = os.path.join(section_dir, "agent_library.json")
            section_personas = [p for p in library["personas"]
                                if p.get("source_path") == section_path]
            self.assertEqual(len(section_personas), 1, name)
            self.assertIn("preset_params", section_personas[0], name)
            self.assertEqual(warnings, [], name)

    def test_each_section_mints_chat_and_publishes_from_real_close_hook(self):
        root = tempfile.mkdtemp(prefix="sections-8.3-smoke-")
        self.addCleanup(shutil.rmtree, root, True)
        for name in NAMES:
            project = "project-%s" % name
            sid = "%s/%s/chat-1" % (project, name)
            app_dir = orch.create_session(root, sid, "exercise the section")
            section = seclib.load_section(name, HERE, app_dir=app_dir)
            phase = section.workflow.phases[0]
            block = dict(PUBLISH_BLOCKS[name], body="Evidence-backed output")
            final_output = "```artifact-json\n%s\n```" % json.dumps(block)
            cfg = {"root": root, "_app_dir": app_dir,
                   "_workflow_target": section.workflow.target}
            orch._hook_artifact_publish(
                cfg, project, app_dir, phase, {}, key=phase.key,
                md_path=os.path.join(app_dir, phase.file), transcript="",
                final_output=final_output, coord=None, active=[],
                is_build=False, is_verify_repair=False, allow_writes=False,
                _needs_vlabel=False, consensus=True)
            metas = artlib.list_artifacts(os.path.join(root, project))
            self.assertEqual(len(metas), 1, name)
            self.assertEqual(metas[0]["type"], block["type"], name)
            self.assertEqual(metas[0]["source"]["section"], name)

    def test_corrupt_manifest_surfaces_fallback_banner(self):
        orch_dir = tempfile.mkdtemp(prefix="sections-8.3-corrupt-")
        self.addCleanup(shutil.rmtree, orch_dir, True)
        section_dir = os.path.join(orch_dir, "sections", "design")
        os.makedirs(section_dir)
        with open(os.path.join(section_dir, "section.json"), "w") as fh:
            fh.write("{broken")
        app_dir = os.path.join(orch_dir, "app")
        os.makedirs(app_dir)
        section = seclib.load_section("design", orch_dir, app_dir=app_dir)
        self.assertEqual(section.id, "design")
        self.assertEqual(len(evlib.read_events(
            app_dir, kinds=["config_fallback"])), 1)


if __name__ == "__main__":
    unittest.main()
