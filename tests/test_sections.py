"""V3 board 3.1: section manifests — seed-then-disk-wins, all-or-default,
and the VISIBLE config_fallback banner (ground rule 4: no silent fallback).
"""
import json
import os
import shutil
import tempfile
import unittest

import events as evlib
import sections as seclib
import workflows as wflib


class SectionBase(unittest.TestCase):
    def setUp(self):
        self.orch = tempfile.mkdtemp(prefix="orch_sections_")
        self.app_dir = tempfile.mkdtemp(prefix="orch_sections_app_")

    def tearDown(self):
        shutil.rmtree(self.orch, ignore_errors=True)
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def banners(self):
        return evlib.read_events(self.app_dir, kinds=["config_fallback"])

    def write_manifest(self, name, obj_or_text):
        path = seclib.section_path(name, self.orch)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(obj_or_text, str):
                fh.write(obj_or_text)
            else:
                json.dump(obj_or_text, fh)


class TestLoad(SectionBase):
    def test_happy_path_named_workflow(self):
        self.write_manifest("ideas", {
            "id": "ideas", "title": "Ideas", "workflow": "chat_ideas",
            "default_mode": "auto",
            "artifact_types_emitted": ["idea_batch"],
            "artifact_types_accepted": [], "dod_tier": "light"})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.id, "ideas")
        self.assertEqual(s.workflow_name, "chat_ideas")
        self.assertEqual(s.workflow.name, "chat_ideas")
        self.assertEqual(s.default_mode, "auto")
        self.assertEqual(s.dod_tier, "light")
        self.assertEqual(self.banners(), [], "healthy loads emit NO banner")

    def test_inline_workflow_resolves(self):
        inline = wflib.load_workflow("chat_ideas").to_json()
        inline["name"] = "custom-inline"
        self.write_manifest("ideas", {
            "id": "ideas", "title": "Ideas", "workflow": inline})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.workflow_name, "(inline)")
        self.assertEqual(s.workflow.name, "custom-inline")
        self.assertEqual(self.banners(), [])

    def test_unknown_fields_survive_load_save_cycle(self):
        self.write_manifest("ideas", {
            "id": "ideas", "title": "Ideas", "workflow": "chat_ideas",
            "future_field": {"nested": True}, "another": 7})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        out = s.to_json()
        self.assertEqual(out["future_field"], {"nested": True})
        self.assertEqual(out["another"], 7)

    def test_disk_wins_over_builtin(self):
        seclib.ensure_seeded_sections(self.orch)
        self.write_manifest("ideas", {
            "id": "ideas", "title": "My Renamed Ideas",
            "workflow": "chat_ideas"})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.title, "My Renamed Ideas")


class TestAllOrDefault(SectionBase):
    def test_corrupt_json_yields_full_default_plus_one_banner(self):
        self.write_manifest("ideas", "{not json at all")
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.title, "Ideas", "the FULL built-in default")
        self.assertEqual(s.workflow_name, "chat_ideas")
        evts = self.banners()
        self.assertEqual(len(evts), 1, "exactly one banner per substitution")
        self.assertEqual(evts[0]["section"], "ideas")
        self.assertIn("section.json", evts[0]["file"])
        self.assertTrue(evts[0]["error"], "the error must be specific")

    def test_half_valid_manifest_never_half_applies(self):
        # title present but workflow malformed: NOTHING of the user file
        # may leak into the returned object (all-or-default).
        self.write_manifest("ideas", {
            "id": "ideas", "title": "Half Applied", "workflow": 42})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.title, "Ideas",
                         "a corrupt manifest must not partially apply")
        self.assertEqual(len(self.banners()), 1)

    def test_missing_required_field_defaults_with_banner(self):
        self.write_manifest("ideas", {"id": "ideas", "workflow": "chat_ideas"})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.title, "Ideas")
        evts = self.banners()
        self.assertIn("title", evts[0]["error"])

    def test_unknown_section_without_builtin_banners(self):
        s = seclib.load_section("mystery", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.id, "mystery")
        self.assertEqual(len(self.banners()), 1)

    def test_unknown_named_workflow_banners_but_keeps_manifest(self):
        self.write_manifest("ideas", {
            "id": "ideas", "title": "Kept Title",
            "workflow": "no-such-workflow"})
        s = seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        self.assertEqual(s.title, "Kept Title",
                         "a valid manifest with an unknown workflow keeps "
                         "its other fields — only the workflow defaulted")
        evts = self.banners()
        self.assertEqual(len(evts), 1)
        self.assertIn("no-such-workflow", evts[0]["error"])

    def test_banner_kind_is_registered(self):
        self.assertIn("config_fallback", evlib.KINDS)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertTrue(evlib.emit_event(
                self.app_dir, "config_fallback", section="s", file="f",
                error="e"))

    def test_banner_line_respects_the_cap(self):
        self.write_manifest("ideas", "{" + "x" * 8000)
        seclib.load_section("ideas", self.orch, app_dir=self.app_dir)
        with open(evlib.events_path(self.app_dir), encoding="utf-8") as fh:
            line = fh.readline()
        self.assertLessEqual(len(line.encode("utf-8")), 3501)
        json.loads(line)


class TestSeeding(SectionBase):
    def test_seeds_only_when_absent_and_is_idempotent(self):
        seclib.ensure_seeded_sections(self.orch)
        path = seclib.section_path("ideas", self.orch)
        self.assertTrue(os.path.exists(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("USER EDIT — even invalid, never clobbered")
        seclib.ensure_seeded_sections(self.orch)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("USER EDIT", fh.read())

    def test_list_sections_unions_disk_and_builtins(self):
        self.write_manifest("customsec", {
            "id": "customsec", "title": "C", "workflow": "chat_ideas"})
        names = seclib.list_sections(self.orch)
        self.assertIn("ideas", names)
        self.assertIn("research", names)
        self.assertIn("customsec", names)
        self.assertEqual(names, sorted(names))


if __name__ == "__main__":
    unittest.main()
