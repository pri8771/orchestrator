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
        self.assertEqual(s.workflow_name, "brainstorm")
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


class TestSectionRoles(SectionBase):
    """V3 board 3.4: section-first role pools with whole-pool precedence,
    invalid-pool fall-through + banner, deep-copy isolation."""

    def _sdir(self):
        d = os.path.join(self.orch, "sections", "research")
        os.makedirs(d, exist_ok=True)
        return d

    def _write_roles(self, obj_or_text):
        import json as _json
        path = os.path.join(self._sdir(), "roles.json")
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(obj_or_text, str):
                fh.write(obj_or_text)
            else:
                _json.dump(obj_or_text, fh)
        return path

    def test_valid_section_pool_replaces_whole_pool(self):
        import roles as roleslib
        self._write_roles({"personalities": [
            {"id": "sk", "name": "Section Skeptic", "style": "s"}]})
        p, r = roleslib.load_roles_layered(section_dir=self._sdir())
        self.assertEqual([x["name"] for x in p], ["Section Skeptic"],
                         "whole-pool replacement, never a merge")
        _bp, base_r = roleslib.load_roles()
        self.assertEqual([x["id"] for x in r],
                         [x["id"] for x in base_r],
                         "the absent pool inherits the next layer")

    def test_invalid_pool_falls_through_with_banner(self):
        import roles as roleslib
        path = self._write_roles({"personalities": [{"name": "no id"}]})
        events = []
        p, _r = roleslib.load_roles_layered(
            section_dir=self._sdir(),
            on_fallback=lambda pth, err: events.append((pth, str(err))))
        self.assertEqual([x["id"] for x in p],
                         [x["id"] for x in roleslib.DEFAULT_PERSONALITIES])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], path)
        self.assertIn("personalities", events[0][1])

    def test_corrupt_file_falls_through_with_banner(self):
        import roles as roleslib
        self._write_roles("{broken")
        events = []
        p, r = roleslib.load_roles_layered(
            section_dir=self._sdir(),
            on_fallback=lambda pth, err: events.append(pth))
        self.assertEqual(len(events), 1)
        self.assertTrue(p and r)

    def test_absent_file_is_silent(self):
        import roles as roleslib
        events = []
        roleslib.load_roles_layered(
            section_dir=os.path.join(self.orch, "sections", "nope"),
            on_fallback=lambda pth, err: events.append(pth))
        self.assertEqual(events, [])

    def test_deepcopy_isolation(self):
        import roles as roleslib
        self._write_roles({"personalities": [
            {"id": "sk", "name": "Mutate Me", "style": "s"}]})
        p1, _ = roleslib.load_roles_layered(section_dir=self._sdir())
        p1[0]["name"] = "CORRUPTED"
        p2, _ = roleslib.load_roles_layered(section_dir=self._sdir())
        self.assertEqual(p2[0]["name"], "Mutate Me",
                         "mutating one resolved cast bled into another")
        d1, _ = roleslib.load_roles()
        d1[0]["name"] = "CORRUPTED DEFAULT"
        d2, _ = roleslib.load_roles()
        self.assertNotEqual(d2[0]["name"], "CORRUPTED DEFAULT")

    def test_section_agent_overrides_win_per_agent(self):
        import roles as roleslib
        self._write_roles({"agent_role_overrides": {"codex": "sec-role"}})
        merged = roleslib.load_agent_role_overrides_layered(
            section_dir=self._sdir())
        self.assertEqual(merged.get("codex"), "sec-role")


class TestShippedSections(unittest.TestCase):
    """V3 board 3.6: the four shipped sections load clean, match their
    builtins, and serve their layers end-to-end from JSON alone."""

    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    NAMES = ("ideas", "research", "qa", "planning")

    def test_all_four_load_with_zero_banners(self):
        app_dir = tempfile.mkdtemp(prefix="orch_shipped_")
        self.addCleanup(shutil.rmtree, app_dir, True)
        for name in self.NAMES:
            s = seclib.load_section(name, self.HERE, app_dir=app_dir)
            self.assertEqual(s.id, name)
            self.assertTrue(s.workflow.phases, "workflow must resolve")
        self.assertEqual(
            evlib.read_events(app_dir, kinds=["config_fallback"]), [],
            "shipped manifests must load with ZERO banners")

    def test_builtins_match_the_shipped_files(self):
        for name in self.NAMES:
            with open(os.path.join(self.HERE, "sections", name,
                                   "section.json"), encoding="utf-8") as fh:
                disk = json.load(fh)
            self.assertEqual(seclib._BUILTINS[name], disk,
                             "%s builtin drifted from the shipped file" % name)

    def test_ideas_keeps_app_target_for_global_rules(self):
        s = seclib.load_section("ideas", self.HERE)
        self.assertEqual(s.workflow.target, "app",
                         "brainstorm behavior must match today exactly")

    def test_planning_contracts_are_byte_equal_to_defaults(self):
        with open(os.path.join(self.HERE, "sections", "planning",
                               "contracts.json"), encoding="utf-8") as fh:
            shipped = {(e["phase_key"], e["contract"]): e["prompt_snippet"]
                       for e in json.load(fh)["contracts"]}
        defaults = {(e["phase_key"], e["contract"]): e["prompt_snippet"]
                    for e in seclib.DEFAULT_CONTRACTS
                    if e["phase_key"] in ("app_features", "tech_specs",
                                          "task_assignments")}
        self.assertEqual(shipped, defaults)

    def test_section_rules_serve_through_the_layered_lookup(self):
        import phase_rules as pr
        for name, phase in (("research", "gather"), ("qa", "security")):
            layer = os.path.join(self.HERE, "sections", name, "rules.json")
            layered = pr.render_phase_playbook(self.HERE, "research", phase,
                                               layers=[layer])
            flat = pr.render_phase_playbook(self.HERE, "research", phase)
            self.assertEqual(layered, flat,
                             "seeded rules copy the global entries — the "
                             "layer must serve identical text (deletion of "
                             "global entries is 8.8, not this card)")
            self.assertTrue(layered, "the section layer must serve rules")

    def test_section_cast_serves_through_the_layered_loader(self):
        import roles as roleslib
        p, r = roleslib.load_roles_layered(
            self.HERE,
            section_dir=os.path.join(self.HERE, "sections", "qa"))
        self.assertEqual([x["id"] for x in p],
                         ["skeptic", "pragmatist", "systems_thinker"])
        self.assertEqual([x["id"] for x in r],
                         ["qa", "security", "red_team"],
                         "QA debates with the red-team cast")

    def test_global_phase_rules_entries_survive(self):
        import phase_rules as pr
        rules = pr.load_rules(self.HERE)
        for key in ("gather", "security", "app_features", "recon"):
            self.assertIn(key, rules["phases"],
                          "global entries must NOT be removed this milestone")
