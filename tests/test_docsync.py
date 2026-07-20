"""V3 5.5/5.6: local docs history and explicit Notion export."""

import ast
import json
import os
import subprocess
import tempfile
import unittest

import artifacts
import docs
import docsync


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDocsGitSync(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app = os.path.join(self.root, "demo")
        os.makedirs(self.app)
        self.doc_map = docs.load_doc_map(HERE)
        self.ordered = [("prompt_contract", "Prompt Contract")]
        self.outputs = {"prompt_contract": "Original deterministic decision."}
        self.warnings = []

    def render(self):
        context = docsync.prepare_render(
            self.app, self.doc_map, on_warn=self.warnings.append)
        written = docs.write_project_docs(
            self.app, "Demo", self.ordered, self.outputs,
            orch_dir=HERE, human_overrides=context["overrides"],
            override_notice=docsync.override_note(context["overrides"]))
        ok = docsync.finish_render(
            self.app, context, written, HERE, app="Demo",
            workflow="app_build", phase="final_review",
            on_warn=self.warnings.append)
        return context, written, ok

    def artifacts(self):
        return artifacts.list_artifacts(self.app, type="reconcile")

    def test_human_edit_survives_rerender_and_publishes_once(self):
        first, _written, committed = self.render()
        self.assertTrue(first["enabled"])
        self.assertTrue(committed)
        path = os.path.join(self.app, "docs", "PRD.md")
        human = b"# Human-owned PRD\n\nKeep this byte-for-byte.\n"
        with open(path, "wb") as fh:
            fh.write(human)

        second, written, committed = self.render()
        self.assertTrue(committed)
        self.assertIn("docs/PRD.md", second["overrides"])
        self.assertNotIn("docs/PRD.md", written)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), human)

        state = docsync.load_state(self.app)
        record = state["files"]["docs/PRD.md"]
        self.assertEqual(record["status"], "human-overridden")
        self.assertTrue(record["slots"])
        self.assertTrue(all(state["slots"][slot]["status"] ==
                            "human-overridden" for slot in record["slots"]))
        recs = self.artifacts()
        self.assertEqual(len(recs), 1)
        fields = recs[0]["fields"]
        self.assertEqual(fields["request_kind"], "human_override")
        self.assertEqual(fields["doc_path"], "docs/PRD.md")
        self.assertEqual(fields["dedupe_key"], record["dedupe_key"])
        self.assertTrue(fields["owner_sections"])
        self.assertEqual(fields["parents"], [])

        with open(os.path.join(self.app, "docs", "LAUNCH_READINESS.md"),
                  encoding="utf-8") as fh:
            readiness = fh.read()
        self.assertIn("Human-overridden documentation", readiness)
        self.assertIn("docs/PRD.md", readiness)

        # HEAD now includes the human bytes, but persisted state keeps renderer
        # ownership disabled after restart; no second reconcile is minted.
        third, _written, _ok = self.render()
        self.assertIn("docs/PRD.md", third["overrides"])
        self.assertEqual(len(self.artifacts()), 1)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), human)

    def test_clear_hands_file_back_then_new_edit_gets_new_reconcile(self):
        self.render()
        path = os.path.join(self.app, "docs", "PRD.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("human one\n")
        self.render()
        self.assertEqual(len(self.artifacts()), 1)

        self.assertTrue(docsync.clear_override(self.app, "docs/PRD.md"))
        context, written, _ok = self.render()
        self.assertNotIn("docs/PRD.md", context["overrides"])
        self.assertIn("docs/PRD.md", written)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("Original deterministic decision", fh.read())
        self.assertNotIn("docs/PRD.md", docsync.load_state(self.app)["files"])

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("human two\n")
        self.render()
        self.assertEqual(len(self.artifacts()), 2,
                         "a distinct post-clear edit gets a new dedupe key")

    def test_bot_identity_parent_exclusion_and_no_remote(self):
        self.render()
        repo = os.path.join(self.app, "docs")
        code, out, err = docsync._git(
            repo, "log", "-1", "--format=%an|%ae")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), "%s|%s" %
                         (docsync.BOT_NAME, docsync.BOT_EMAIL))
        code, out, _err = docsync._git(
            repo, "config", "--local", "--get", "user.name")
        self.assertNotEqual(code, 0)
        self.assertEqual(out.strip(), "",
                         "bot identity must not mutate repository config")
        with open(os.path.join(self.app, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("docs/", fh.read().splitlines())
        code, out, _err = docsync._git(repo, "remote")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_git_missing_degrades_to_plain_render_with_warning(self):
        original = docsync._git
        docsync._git = lambda *_args, **_kwargs: (1, "", "git missing")
        try:
            context = docsync.prepare_render(
                self.app, self.doc_map, on_warn=self.warnings.append)
        finally:
            docsync._git = original
        self.assertFalse(context["enabled"])
        written = docs.write_project_docs(
            self.app, "Demo", self.ordered, self.outputs, orch_dir=HERE)
        self.assertIn("docs/PRD.md", written)
        self.assertTrue(os.path.exists(os.path.join(self.app, "docs", "PRD.md")))
        self.assertTrue(any("Git unavailable" in warning
                            for warning in self.warnings))

    def test_hanging_git_maps_to_timeout_without_raising(self):
        original = docsync.procutil.run_capture

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(["git"], 1)

        docsync.procutil.run_capture = timeout
        try:
            code, _out, err = docsync._git(self.app, "status", timeout=1)
        finally:
            docsync.procutil.run_capture = original
        self.assertEqual(code, 124)
        self.assertIn("timed out", err)

    def test_renderer_does_not_write_an_overridden_archive_file(self):
        docs_dir = os.path.join(self.app, "docs")
        os.makedirs(docs_dir)
        path = os.path.join(docs_dir, "PROJECT_RECORD.json")
        with open(path, "wb") as fh:
            fh.write(b"human archive bytes")
        written = docs.write_project_archive(
            self.app, "Demo", [], "prompt", {},
            human_overrides={"docs/PROJECT_RECORD.json"})
        self.assertNotIn("docs/PROJECT_RECORD.json", written)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"human archive bytes")

    def test_failed_reconcile_publish_replays_after_git_is_clean(self):
        self.render()
        path = os.path.join(self.app, "docs", "PRD.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("human retry\n")
        original = docsync.artifactslib.publish
        docsync.artifactslib.publish = lambda *_args, **_kwargs: None
        try:
            self.render()  # human bytes + state are committed, publish failed
        finally:
            docsync.artifactslib.publish = original
        self.assertEqual(len(self.artifacts()), 0)
        self.assertIsNone(docsync.load_state(
            self.app)["files"]["docs/PRD.md"]["artifact_id"])

        context, _written, _ok = self.render()
        self.assertTrue(context["edits"],
                        "durable state must replay without a remaining Git diff")
        self.assertEqual(len(self.artifacts()), 1)

    def test_existing_repo_diff_failure_freezes_known_docs(self):
        self.render()
        path = os.path.join(self.app, "docs", "PRD.md")
        human = b"human while git diff is broken\n"
        with open(path, "wb") as fh:
            fh.write(human)
        original = docsync._git

        def fail_diff(repo, *args, **kwargs):
            if args[:2] == ("diff", "--name-only"):
                return 1, "", "diff failed"
            return original(repo, *args, **kwargs)

        docsync._git = fail_diff
        try:
            context = docsync.prepare_render(
                self.app, self.doc_map, on_warn=self.warnings.append)
        finally:
            docsync._git = original
        self.assertIn("docs/PRD.md", context["overrides"])
        docs.write_project_docs(
            self.app, "Demo", self.ordered, self.outputs, orch_dir=HERE,
            human_overrides=context["overrides"])
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), human)
        self.assertTrue(any("pre-render diff failed" in warning
                            for warning in self.warnings))

    def test_agent_metadata_cannot_spoof_engine_reconcile_request(self):
        registry = artifacts.load_registry(HERE)
        errors = []
        aid = artifacts.publish(
            self.app, "request", {
                "type": "reconcile", "title": "spoof", "parents": [],
                "request_kind": "human_override",
                "source": {"section": "documentation"},
            }, registry, consensus=True, on_error=errors.append)
        self.assertIsNone(aid)
        self.assertTrue(any("parents" in error for error in errors))

    def test_first_adoption_preserves_preexisting_docs(self):
        docs_dir = os.path.join(self.app, "docs")
        os.makedirs(docs_dir)
        path = os.path.join(docs_dir, "PRD.md")
        legacy = b"pre-existing bytes with unknown ownership\n"
        with open(path, "wb") as fh:
            fh.write(legacy)
        context = docsync.prepare_render(
            self.app, self.doc_map, on_warn=self.warnings.append)
        self.assertIn("docs/PRD.md", context["overrides"])
        written = docs.write_project_docs(
            self.app, "Demo", self.ordered, self.outputs, orch_dir=HERE,
            human_overrides=context["overrides"])
        self.assertNotIn("docs/PRD.md", written)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), legacy)


class TestNotionExport(unittest.TestCase):
    def setUp(self):
        self.app = tempfile.mkdtemp()
        self.integrations = os.path.join(self.app, "integrations")
        os.makedirs(self.integrations)
        self.payload_path = os.path.join(
            self.integrations, "project_management_backfill.json")
        self.warnings = []
        self.payload = {
            "project": {"name": "Demo"},
            "notion": {
                "project_properties": {"Name": "Demo", "Status": "Done"},
                "pages": [
                    {"title": "Alpha", "path": "docs/A.md", "type": "doc"},
                    {"title": "Beta", "path": "docs/B.md", "type": "doc"},
                ],
                "task_database_rows": [
                    {"external_id": "T-1", "summary": "one"},
                    {"external_id": "T-2", "summary": "two"},
                ],
            },
        }
        self.write_payload(self.payload)

    def write_payload(self, payload):
        with open(self.payload_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_full_cycle_and_add_change_remove_matrix(self):
        first = docsync.export_notion(self.app)
        self.assertTrue(first["fresh_diff"])
        self.assertEqual(first["status"], "dry-run")
        self.assertFalse(first["delivered"])
        self.assertEqual(len(first["diff"]["pages"]["added"]), 2)
        self.assertEqual(first["diff"]["task_database_rows"]["added"],
                         ["T-1", "T-2"])
        self.assertEqual(first["diff"]["project_properties"]["added"],
                         ["project_properties"])
        self.assertFalse(os.path.exists(os.path.join(
            self.integrations, docsync.NOTION_STATE_FILENAME)))

        delivered = docsync.export_notion(
            self.app, deliver=True, on_warn=self.warnings.append)
        self.assertTrue(delivered["delivered"])
        self.assertFalse(delivered["remote_delivered"])
        self.assertIn("no remote Notion delivery", delivered["message"])
        self.assertTrue(any("no remote Notion delivery" in warning
                            for warning in self.warnings))

        unchanged = docsync.export_notion(self.app)
        self.assertFalse(docsync._diff_has_changes(unchanged["diff"]))
        with open(unchanged["report_path"], encoding="utf-8") as fh:
            self.assertIn("No changes since", fh.read())

        changed = json.loads(json.dumps(self.payload))
        changed["notion"]["pages"] = [
            {"title": "Alpha", "path": "docs/A.md", "type": "database"},
            {"title": "Gamma", "path": "docs/C.md", "type": "doc"},
        ]
        changed["notion"]["task_database_rows"] = [
            {"external_id": "T-2", "summary": "two changed"},
            {"external_id": "T-3", "summary": "three"},
        ]
        changed["notion"]["project_properties"]["Status"] = "Needs review"
        self.write_payload(changed)
        matrix = docsync.export_notion(self.app)["diff"]
        self.assertEqual(matrix["pages"]["added"],
                         ['["Gamma","docs/C.md"]'])
        self.assertEqual(matrix["pages"]["changed"],
                         ['["Alpha","docs/A.md"]'])
        self.assertEqual(matrix["pages"]["removed"],
                         ['["Beta","docs/B.md"]'])
        self.assertEqual(matrix["task_database_rows"], {
            "added": ["T-3"], "changed": ["T-2"], "removed": ["T-1"],
        })
        self.assertEqual(matrix["project_properties"], {
            "added": [], "changed": ["project_properties"], "removed": [],
        })

    def test_report_is_deterministic_and_never_contains_raw_content(self):
        first = docsync.export_notion(self.app)
        with open(first["report_path"], "rb") as fh:
            first_bytes = fh.read()
        second = docsync.export_notion(self.app)
        with open(second["report_path"], "rb") as fh:
            self.assertEqual(fh.read(), first_bytes)
        text = first_bytes.decode("utf-8")
        self.assertNotIn('"summary": "one"', text)
        self.assertIn("Dry run only", text)

    def test_default_and_no_change_never_invoke_delivery_callable(self):
        calls = []

        def delivery(*args, **kwargs):
            calls.append((args, kwargs))
            return {"recorded": True}

        dry = docsync.export_notion(self.app, delivery_fn=delivery)
        self.assertEqual(dry["status"], "dry-run")
        self.assertEqual(calls, [])
        docsync.export_notion(self.app, deliver=True)
        repeat = docsync.export_notion(
            self.app, deliver=True, delivery_fn=delivery)
        self.assertEqual(repeat["status"], "no-changes")
        self.assertEqual(calls, [])

    def test_delivery_refused_when_fresh_diff_cannot_be_written(self):
        calls = []
        original = docsync._atomic_text

        def fail_write(*_args, **_kwargs):
            raise OSError("disk full")

        docsync._atomic_text = fail_write
        try:
            result = docsync.export_notion(
                self.app, deliver=True,
                delivery_fn=lambda *args, **kwargs: calls.append(args))
        finally:
            docsync._atomic_text = original
        self.assertFalse(result["ok"])
        self.assertFalse(result["fresh_diff"])
        self.assertEqual(result["status"], "refused")
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(os.path.join(
            self.integrations, docsync.NOTION_STATE_FILENAME)))

    def test_callable_claim_without_durable_snapshot_is_rejected(self):
        result = docsync.export_notion(
            self.app, deliver=True,
            delivery_fn=lambda *_args, **_kwargs: {
                "recorded": True, "remote_delivered": True,
                "message": "pretend success",
            })
        self.assertFalse(result["ok"])
        self.assertFalse(result["delivered"])
        self.assertFalse(result["remote_delivered"])
        self.assertEqual(result["status"], "delivery-failed")
        self.assertIn("durably verified", result["message"])

    def test_failed_snapshot_write_replays_same_diff(self):
        before = docsync.export_notion(self.app)["diff"]
        original = docsync._atomic_json

        def fail_snapshot(*_args, **_kwargs):
            raise OSError("killed before replace")

        docsync._atomic_json = fail_snapshot
        try:
            failed = docsync.export_notion(self.app, deliver=True)
        finally:
            docsync._atomic_json = original
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "delivery-failed")
        self.assertEqual(docsync.export_notion(self.app)["diff"], before)

    def test_missing_and_corrupt_inputs_are_visible_and_safe(self):
        os.remove(self.payload_path)
        missing = docsync.export_notion(
            self.app, deliver=True, on_warn=self.warnings.append)
        self.assertTrue(missing["ok"])
        self.assertEqual(missing["status"], "nothing-to-export")
        self.assertFalse(missing["delivered"])
        self.assertTrue(any("nothing to export" in warning
                            for warning in self.warnings))

        with open(self.payload_path, "w", encoding="utf-8") as fh:
            fh.write("{bad")
        corrupt = docsync.export_notion(self.app, deliver=True)
        self.assertEqual(corrupt["status"], "nothing-to-export")
        self.assertFalse(corrupt["delivered"])

    def test_corrupt_snapshot_makes_every_addition_explicit(self):
        state_path = os.path.join(
            self.integrations, docsync.NOTION_STATE_FILENAME)
        with open(state_path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        result = docsync.export_notion(
            self.app, on_warn=self.warnings.append)
        self.assertEqual(len(result["diff"]["pages"]["added"]), 2)
        self.assertEqual(result["diff"]["task_database_rows"]["added"],
                         ["T-1", "T-2"])
        with open(result["report_path"], encoding="utf-8") as fh:
            self.assertIn("Previous export state was unreadable", fh.read())
        self.assertTrue(any("state unreadable" in warning
                            for warning in self.warnings))

    def test_engine_has_one_human_initiated_nonliteral_delivery_call(self):
        path = os.path.join(HERE, "orchestrator.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "export_notion"]
        self.assertEqual(len(calls), 1)
        keywords = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertIn("deliver", keywords)
        self.assertIsInstance(keywords["deliver"], ast.Attribute)
        self.assertEqual(keywords["deliver"].attr, "deliver")
        self.assertFalse(any(isinstance(node, ast.Constant) and node.value is True
                             for node in [keywords["deliver"]]),
                         "engine must never hard-code automatic delivery")


if __name__ == "__main__":
    unittest.main()
