"""V3 8.8 one-shot tuned-config migration safety and behavior parity."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import migrate_v3
import modelrouting
import phase_rules
import sections


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _tree_hash(root):
    digest = hashlib.sha256()
    for cur, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(cur, name)
            digest.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


class MigratorTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="orch_migrate_v3_")
        shutil.copytree(os.path.join(migrate_v3.HERE, "workflows"),
                        os.path.join(self.workspace, "workflows"))
        shutil.copytree(os.path.join(migrate_v3.HERE, "sections"),
                        os.path.join(self.workspace, "sections"))
        for name in ("phase_rules.json", "model_routing.json"):
            shutil.copy2(os.path.join(migrate_v3.HERE, name),
                         os.path.join(self.workspace, name))

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def tune_trio(self):
        workflow_path = os.path.join(
            self.workspace, "workflows", "brainstorm.json")
        workflow = _read(workflow_path)
        workflow["phases"][0]["purpose"] += " MIGRATED-WORKFLOW-PURPOSE"
        _write(workflow_path, workflow)

        rules_path = os.path.join(self.workspace, "phase_rules.json")
        rules = _read(rules_path)
        rules["phases"]["prompt_contract"]["rules"].append(
            "MIGRATED-RULE-EVIDENCE")
        _write(rules_path, rules)

        routing_path = os.path.join(self.workspace, "model_routing.json")
        routing = _read(routing_path)
        routing.setdefault("phases", {})["prompt_contract"] = {
            "codex": "migration-model", "codex_reasoning": "high"}
        _write(routing_path, routing)
        return workflow_path, rules_path, routing_path

    def test_pinned_no_git_seed_hashes_match_every_shipped_source(self):
        expected = {"phase_rules.json", "model_routing.json"} | {
            "workflows/" + name for name in os.listdir(
                os.path.join(migrate_v3.HERE, "workflows"))
            if name.endswith(".json")}
        self.assertEqual(set(migrate_v3.SEED_HASHES), expected)
        for rel, digest in migrate_v3.SEED_HASHES.items():
            self.assertEqual(
                migrate_v3._file_hash(os.path.join(migrate_v3.HERE, rel)),
                digest,
                "%s no longer matches its SEED_HASHES pin. If the seed change "
                "is intended, re-pin it IN THE SAME COMMIT (protocol comment "
                "above SEED_HASHES in migrate_v3.py); otherwise revert the "
                "file. An un-re-pinned seed silently downgrades no-git "
                "installs to whole-document carry." % rel)

    def test_pristine_real_configs_are_all_seed_identical_and_dry_run_writes_zero(self):
        before = _tree_hash(self.workspace)
        plan = migrate_v3.build_plan(self.workspace)
        report = migrate_v3.render_plan(plan)
        self.assertEqual(_tree_hash(self.workspace), before)
        self.assertEqual(plan["targets"], {})
        self.assertEqual(plan["unmapped"], [])
        self.assertEqual(len(plan["seed_identical"]),
                         len(migrate_v3.SEED_HASHES))
        self.assertIn("no tuning, section seed wins", report)
        self.assertIn("DRY-RUN — no files changed", report)

        proc = subprocess.run(
            [sys.executable, os.path.join(migrate_v3.HERE, "migrate_v3.py"),
             "--workspace", self.workspace],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_tree_hash(self.workspace), before,
                         "CLI dry-run must not create receipt/tmp/backup files")

    def test_tuned_trio_maps_to_loadable_section_and_preserves_behavior(self):
        workflow_path, rules_path, routing_path = self.tune_trio()
        app = os.path.join(self.workspace, "project-a")
        os.makedirs(app)
        overlay_path = os.path.join(app, "model_routing.json")
        overlay = {"phases": {"other": {"codex": "project-model"}}}
        _write(overlay_path, overlay)
        overlay_before = _read_bytes(overlay_path)

        pre_playbook = phase_rules.render_phase_playbook(
            self.workspace, "app", "prompt_contract")
        pre_routing = modelrouting.overrides_for(
            modelrouting.load_routing(self.workspace), "prompt_contract")
        plan = migrate_v3.build_plan(self.workspace)
        report = migrate_v3.render_plan(plan)
        self.assertFalse(plan["unmapped"], report)
        self.assertIn("project-a/model_routing.json: per-app overlay detected",
                      report)
        self.assertIn("MIGRATED-RULE-EVIDENCE", report)
        self.assertIn("migration-model", report)
        receipt = migrate_v3.apply_plan(plan)

        ideas = sections.load_section("ideas", self.workspace)
        self.assertEqual(ideas.workflow_name, "(inline)")
        self.assertIn("MIGRATED-WORKFLOW-PURPOSE",
                      ideas.workflow.phases[0].purpose)
        self.assertEqual(sections.lint_section("ideas", self.workspace), [])

        section_dir = os.path.join(self.workspace, "sections", "ideas")
        migrated_rules = _read(os.path.join(section_dir, "rules.json"))
        self.assertIn("MIGRATED-RULE-EVIDENCE",
                      migrated_rules["phases"]["prompt_contract"]["rules"])
        migrated_routing = _read(os.path.join(
            section_dir, "model_routing.json"))
        self.assertEqual(migrated_routing["phases"]["prompt_contract"]["codex"],
                         "migration-model")
        post_playbook = phase_rules.render_phase_playbook(
            self.workspace, "app", "prompt_contract",
            layers=[os.path.join(section_dir, "rules.json")])
        post_routing = modelrouting.overrides_for(
            modelrouting.load_routing_for_session(
                self.workspace, app, section_dir=section_dir),
            "prompt_contract")
        self.assertEqual(post_playbook, pre_playbook,
                         "rendered tuned V2 and migrated V3 playbooks drifted")
        self.assertEqual(post_routing, pre_routing,
                         "routing resolution changed through migration")
        self.assertEqual(_read_bytes(overlay_path), overlay_before)
        self.assertEqual(modelrouting.overrides_for(
            modelrouting.load_routing_for_app(self.workspace, app), "other"),
            {"codex": "project-model"})

        for path in (workflow_path, rules_path, routing_path):
            self.assertTrue(os.path.exists(path + ".v2.bak"))
            self.assertEqual(_read(path + ".v2.bak"), _read(path))
        self.assertEqual(receipt["status"], "complete")
        self.assertIn("project-a/model_routing.json",
                      receipt["per_app_overlays_left_in_place"])
        self.assertTrue(os.path.isdir(app),
                        "workspace/project layout belongs to 3.0 and must not move")

    def test_second_run_is_verified_noop_without_duplicate_entries(self):
        self.tune_trio()
        first = migrate_v3.build_plan(self.workspace)
        migrate_v3.apply_plan(first)
        before = _tree_hash(self.workspace)
        second = migrate_v3.build_plan(self.workspace)
        self.assertTrue(second["already_migrated"])
        self.assertEqual(second["targets"], {})
        self.assertIn("already migrated",
                      migrate_v3.render_plan(second).lower())
        migrate_v3.apply_plan(second)
        self.assertEqual(_tree_hash(self.workspace), before)

    def test_unmapped_key_is_loud_blocking_and_allow_keeps_backup_receipt(self):
        path = os.path.join(self.workspace, "workflows", "brainstorm.json")
        doc = _read(path)
        doc["secret_tuning_knob"] = {"must": "survive"}
        _write(path, doc)
        plan = migrate_v3.build_plan(self.workspace)
        self.assertTrue(any("secret_tuning_knob" in x
                            for x in plan["unmapped"]))
        self.assertIn("UNMAPPED", migrate_v3.render_plan(plan))
        with self.assertRaises(migrate_v3.MigrationError):
            migrate_v3.apply_plan(plan)
        self.assertFalse(os.path.exists(path + ".v2.bak"),
                         "refusal happens before any mutation")
        receipt = migrate_v3.apply_plan(plan, allow_unmapped=True)
        self.assertEqual(_read(path + ".v2.bak")["secret_tuning_knob"],
                         {"must": "survive"})
        self.assertTrue(any("secret_tuning_knob" in x
                            for x in receipt["unmapped_allowed"]))

    def test_interrupted_apply_leaves_atomic_target_and_rerun_completes(self):
        self.tune_trio()
        plan = migrate_v3.build_plan(self.workspace)
        seen = []

        def kill_after_first(rel):
            seen.append(rel)
            if len(seen) == 1:
                raise RuntimeError("simulated kill between target files")

        with self.assertRaises(RuntimeError):
            migrate_v3.apply_plan(plan, after_target=kill_after_first)
        self.assertFalse(os.path.exists(os.path.join(
            self.workspace, migrate_v3.RECEIPT)))
        first_path = os.path.join(self.workspace, seen[0])
        self.assertIsInstance(_read(first_path), dict,
                              "written target is whole valid JSON, never torn")
        rerun = migrate_v3.build_plan(self.workspace)
        migrate_v3.apply_plan(rerun)
        final = migrate_v3.build_plan(self.workspace)
        self.assertTrue(final["already_migrated"])
        rules = _read(os.path.join(
            self.workspace, "sections", "ideas", "rules.json"))
        self.assertEqual(rules["phases"]["prompt_contract"]["rules"].count(
            "MIGRATED-RULE-EVIDENCE"), 1)

    def _fake_engine(self):
        """A copy of the shipped seeds posing as the engine checkout, with
        migrate_v3.HERE patched at it for the rest of the test."""
        engine = tempfile.mkdtemp(prefix="orch_fake_engine_")
        self.addCleanup(shutil.rmtree, engine, ignore_errors=True)
        shutil.copytree(os.path.join(migrate_v3.HERE, "workflows"),
                        os.path.join(engine, "workflows"))
        for name in ("phase_rules.json", "model_routing.json"):
            shutil.copy2(os.path.join(migrate_v3.HERE, name),
                         os.path.join(engine, name))
        self.addCleanup(setattr, migrate_v3, "HERE", migrate_v3.HERE)
        migrate_v3.HERE = engine
        return engine

    def _drift_engine_routing(self, engine):
        """GUI-style rewrite of the engine's working-tree model_routing.json:
        drops a documentation block, replaces phases wholesale."""
        path = os.path.join(engine, "model_routing.json")
        drifted = _read(path)
        drifted.pop("_examples", None)
        drifted["phases"] = {"prompt_contract": {"codex": "drifted-model"}}
        _write(path, drifted)

    def test_external_workspace_ignores_drifted_engine_tree_baseline(self):
        # A-23: a rewritten engine working-tree copy must never be diffed as
        # the "seed" for an external workspace — pristine files would mint
        # phantom tuned deltas computed against the drift.
        engine = self._fake_engine()          # no .git: git fallback is dead
        self._drift_engine_routing(engine)
        plan = migrate_v3.build_plan(self.workspace)
        self.assertIn("model_routing.json", plan["seed_identical"])
        self.assertEqual([e for e in plan["entries"]
                          if e["source"] == "model_routing.json"], [])
        self.assertNotIn("model_routing.json", plan["backups"])
        self.assertEqual(plan["targets"], {})

    def test_dirty_engine_tree_falls_back_to_head_seed_for_external_tuning(self):
        # A-23: dirty engine tree over a pristine HEAD — the hash-verified
        # git fallback keeps precise per-delta migration for a TUNED external
        # workspace instead of whole-document carry or phantom deltas.
        engine = self._fake_engine()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t",
               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
        subprocess.run(git + ["init", "-q"], cwd=engine, check=True)
        subprocess.run(git + ["add", "-A"], cwd=engine, check=True)
        subprocess.run(git + ["commit", "-qm", "seed"], cwd=engine,
                       check=True)
        self._drift_engine_routing(engine)
        routing_path = os.path.join(self.workspace, "model_routing.json")
        routing = _read(routing_path)
        routing.setdefault("phases", {})["prompt_contract"] = {
            "codex": "migration-model", "codex_reasoning": "high"}
        _write(routing_path, routing)
        plan = migrate_v3.build_plan(self.workspace)
        # One entry per owning section, but never a phantom (/_examples) or
        # whole-document delta.
        self.assertEqual({e["delta"] for e in plan["entries"]
                          if e["source"] == "model_routing.json"},
                         {"/phases/prompt_contract"})

    def test_refused_apply_rolls_back_to_pre_apply_tree(self):
        # A-24: validation runs against the real loaders only after every
        # target is written, so a lint failure must restore each written
        # byte — previously APPLY REFUSED left a half-migrated workspace.
        self.tune_trio()
        broken = {"name": "broken_flow", "title": "Broken",
                  "description": "x", "target": "chat", "build_phase": None,
                  "budget": None, "overrides": None, "phases": [{}]}
        _write(os.path.join(self.workspace, "workflows", "broken_flow.json"),
               broken)
        before = _tree_hash(self.workspace)
        plan = migrate_v3.build_plan(self.workspace)
        self.assertEqual(plan["unmapped"], [])
        self.assertIn("sections/broken_flow/section.json", plan["targets"])
        with self.assertRaises(migrate_v3.MigrationError):
            migrate_v3.apply_plan(plan)
        self.assertEqual(_tree_hash(self.workspace), before,
                         "APPLY REFUSED must leave the workspace untouched")
        self.assertFalse(os.path.isdir(os.path.join(
            self.workspace, "sections", "broken_flow")))

    def test_loader_valueerror_is_refused_as_migration_error_with_rollback(self):
        # A-24: the sections loaders raise bare ValueError on shape
        # violations; apply must convert to MigrationError (so main() prints
        # APPLY REFUSED, not a traceback) after rolling back.
        self.tune_trio()
        before = _tree_hash(self.workspace)
        real_lint = sections.lint_section
        self.addCleanup(setattr, sections, "lint_section", real_lint)
        def boom(name, orch_dir):
            raise ValueError("synthetic shape violation")
        sections.lint_section = boom
        plan = migrate_v3.build_plan(self.workspace)
        with self.assertRaises(migrate_v3.MigrationError):
            migrate_v3.apply_plan(plan)
        self.assertEqual(_tree_hash(self.workspace), before)

    def test_apply_backs_up_overwritten_target_manifest(self):
        # A-24: pre-existing TARGET files that are merged-into and rewritten
        # get a .v2.bak recorded in the receipt — previously only SOURCE rels
        # were backed up, leaving prior target bytes unrecoverable.
        self.tune_trio()
        manifest_path = os.path.join(self.workspace, "sections", "ideas",
                                     "section.json")
        prior = _read_bytes(manifest_path)
        plan = migrate_v3.build_plan(self.workspace)
        receipt = migrate_v3.apply_plan(plan)
        self.assertEqual(_read_bytes(manifest_path + ".v2.bak"), prior)
        self.assertIn("sections/ideas/section.json", receipt["backups"])

    def test_custom_workflow_gets_data_complete_reviewable_section(self):
        custom = {
            "name": "customer_success", "title": "Customer success",
            "description": "custom", "target": "chat", "build_phase": None,
            "budget": None, "overrides": None,
            "phases": [{"key": "triage", "folder": "triage",
                        "file": "triage.md", "title": "Triage",
                        "purpose": "Keep this exact purpose", "rounds": 2,
                        "roles": [], "writes": False, "reads_target": False,
                        "verify": None, "checkpoint": False,
                        "structurally_required": True,
                        "requires_verification": False, "doc_sections": [],
                        "test_deliverable": None, "conversational": False}]}
        _write(os.path.join(self.workspace, "workflows",
                            "customer_success.json"), custom)
        plan = migrate_v3.build_plan(self.workspace)
        self.assertFalse(plan["unmapped"])
        migrate_v3.apply_plan(plan)
        manifest = _read(os.path.join(
            self.workspace, "sections", "customer_success", "section.json"))
        self.assertEqual(manifest["workflow"], custom)
        self.assertTrue(manifest["migration_review_required"])
        loaded = sections.load_section("customer_success", self.workspace)
        self.assertEqual(loaded.workflow.phases[0].purpose,
                         "Keep this exact purpose")


if __name__ == "__main__":
    unittest.main()
