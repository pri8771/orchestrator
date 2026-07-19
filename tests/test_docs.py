import json, os, tempfile, unittest
import docs


class TestJiraLabelSanitization(unittest.TestCase):
    def test_valid_lane_id_passes_through(self):
        self.assertEqual(docs._jira_label("primary_ui", "lane"), "primary_ui")

    def test_whitespace_is_not_a_valid_jira_label(self):
        # A lane name with whitespace (plausible free-text from an agent) must
        # be sanitized — Jira rejects labels containing spaces.
        label = docs._jira_label("some free text lane", "lane")
        self.assertNotIn(" ", label)

    def test_empty_or_none_falls_back_to_default(self):
        self.assertEqual(docs._jira_label("", "lane"), "lane")
        self.assertEqual(docs._jira_label(None, "lane"), "lane")
        self.assertEqual(docs._jira_label("   ", "lane"), "lane")

    def test_issue_from_task_label_has_no_whitespace(self):
        task = {"id": "T-1", "title": "x", "owner_lane": "bad lane name"}
        issue = docs._issue_from_task(task, "app", "EPIC-1")
        for label in issue["labels"]:
            self.assertNotIn(" ", label)


class TestBackfillPayloadNoneVerifySummary(unittest.TestCase):
    def test_none_verify_summary_does_not_crash(self):
        # verify_summary=None used to crash on .upper() (missing the `or ""`
        # guard every sibling in this file uses).
        payload = docs.render_project_management_backfill(
            "demo", "prompt", [("final_review", "Final Review")], {},
            tasks=[], interfaces=[], verify_summary=None)
        self.assertEqual(payload["notion"]["project_properties"]["Status"],
                         "Needs review")

    def test_verified_summary_still_maps_to_done(self):
        payload = docs.render_project_management_backfill(
            "demo", "prompt", [("final_review", "Final Review")], {},
            tasks=[], interfaces=[], verify_summary="VERIFIED — build passed")
        self.assertEqual(payload["notion"]["project_properties"]["Status"],
                         "Done")


class TestDocsRenderer(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.phases = [("problem_and_vision", "Problem & Vision"),
                       ("build_coordination", "Build"),
                       ("final_review", "Final Review")]
        self.outputs = {
            "problem_and_vision": "A tiny counter app for focus.",
            "build_coordination": "Built ContentView with a counter.",
            # final_review intentionally absent -> N/A
        }
        self.consensus = {"problem_and_vision": True, "build_coordination": True}

    def test_project_documentation_sections_and_na(self):
        md = docs.render_project_documentation("Demo", self.phases, self.outputs)
        self.assertIn("## Problem & Vision", md)
        self.assertIn("A tiny counter app", md)
        self.assertIn("## Final Review", md)
        self.assertIn("_Not applicable", md)  # final_review didn't run

    def test_structured_block_preferred(self):
        outputs = {"problem_and_vision":
                   'intro\n```phase-output-json\n{"phase":"problem_and_vision","doc_sections":{"prd.summary":"Focus timer"}}\n```'}
        md = docs.render_project_documentation("Demo", [("problem_and_vision", "Problem")], outputs)
        self.assertIn("Focus timer", md)  # value preserved verbatim
        self.assertIn("Prd · Summary", md)

    def test_launch_readiness_counts(self):
        md = docs.render_launch_readiness("Demo", self.phases, self.outputs, self.consensus,
                                          verify_summary="VERIFIED (compiled cleanly)")
        self.assertIn("2 / 3", md)   # phases completed
        self.assertIn("VERIFIED", md)

    def test_write_project_docs_creates_files(self):
        written = docs.write_project_docs(self.app_dir, "Demo", self.phases, self.outputs,
                                          consensus_status=self.consensus, verify_summary="UNVERIFIED")
        self.assertIn("docs/PROJECT_DOCUMENTATION.md", written)
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, "docs", "PROJECT_DOCUMENTATION.md")))
        self.assertTrue(os.path.exists(os.path.join(self.app_dir, "docs", "LAUNCH_READINESS.md")))
        with open(os.path.join(self.app_dir, "docs", "phase_outputs.json")) as fh:
            self.assertEqual(json.load(fh)["problem_and_vision"], self.outputs["problem_and_vision"])

    def test_project_archive_records_prompt_discussions_and_backfill(self):
        phase_dir = os.path.join(self.app_dir, "problem_and_vision")
        os.makedirs(phase_dir)
        with open(os.path.join(phase_dir, "problem_and_vision.md"), "w", encoding="utf-8") as fh:
            fh.write("# Demo\n\n## Original Prompt\n\n```\nBuild it.\n```\n\n## Transcript\n\nchat")
        with open(os.path.join(self.app_dir, "tasks.json"), "w", encoding="utf-8") as fh:
            json.dump({"tasks": [{"id": "T-001", "title": "Build core",
                                  "owner_lane": "primary_ui", "files": ["App.swift"],
                                  "depends_on": [], "acceptance_criteria": ["runs"],
                                  "status": "pending"}]}, fh)
        state = {"phase_outputs": self.outputs, "consensus_status": self.consensus,
                 "vote_results": {}}
        written = docs.write_project_archive(
            self.app_dir, "Demo", self.phases, "Build it.", state,
            workflow_name="app_build", verify_summary="VERIFIED (ok)")
        self.assertIn("docs/PROJECT_RECORD.json", written)
        self.assertIn("docs/phase_discussions.json", written)
        self.assertIn("docs/COMPLETE_PROJECT_DOSSIER.md", written)
        self.assertIn("docs/FULL_TRANSCRIPT.txt", written)
        self.assertIn("integrations/project_management_backfill.json", written)
        with open(os.path.join(self.app_dir, "docs", "PROJECT_RECORD.json"),
                  encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["original_prompt"], "Build it.")
        self.assertIn("chat", record["phase_discussions"][0]["discussion_markdown"])
        with open(os.path.join(self.app_dir, "docs", "COMPLETE_PROJECT_DOSSIER.md"),
                  encoding="utf-8") as fh:
            dossier = fh.read()
        self.assertIn("## Full Discussion Transcripts", dossier)
        self.assertIn("chat", dossier)
        with open(os.path.join(self.app_dir, "docs", "FULL_TRANSCRIPT.txt"),
                  encoding="utf-8") as fh:
            transcript = fh.read()
        self.assertIn("FULL ORCHESTRATOR TRANSCRIPT", transcript)
        self.assertIn("ORIGINAL PROMPT", transcript)
        self.assertIn("chat", transcript)
        with open(os.path.join(self.app_dir, "integrations", "project_management_backfill.json"),
                  encoding="utf-8") as fh:
            backfill = json.load(fh)
        self.assertEqual(backfill["jira"]["issues"][0]["external_id"], "T-001")
        self.assertEqual(backfill["notion"]["project_properties"]["Name"], "Demo")
        self.assertTrue(any(p.get("path") == "docs/COMPLETE_PROJECT_DOSSIER.md"
                            for p in backfill["notion"]["pages"]))
        self.assertTrue(any(p.get("path") == "docs/FULL_TRANSCRIPT.txt"
                            for p in backfill["notion"]["pages"]))


class TestSpecDocs(unittest.TestCase):
    """PRD / TECHNICAL_ARCHITECTURE / QA_REPORT / KNOWN_LIMITATIONS (§24)."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp()
        self.phases = [("initial_discussion", "Initial Discussion"),
                       ("app_features", "App Features"),
                       ("design_discussion", "Design Discussion"),
                       ("tech_specs", "Tech Specs"),
                       ("project_plan", "Project Plan"),
                       ("build_coordination", "Build"),
                       ("final_review", "Final Review")]
        self.outputs = {
            "initial_discussion": "A journaling app for climbers.",
            "design_discussion": "Three screens, tab bar.",
            "tech_specs": "SwiftUI + SwiftData.",
            "build_coordination": "Built it.",
            "final_review": "Go, with caveats.",
            # app_features + project_plan intentionally absent -> N/A
        }
        self.consensus = {k: True for k in self.outputs}

    def test_prd_renders_sources_and_na_for_missing(self):
        md = docs.render_composed_doc("Demo", "Product Requirements (PRD)",
                                      docs.PRD_SOURCES, self.phases, self.outputs)
        self.assertIn("A journaling app for climbers.", md)
        self.assertIn("## App Features", md)
        self.assertIn("N/A — this phase did not run", md)
        # next_steps_small isn't in this workflow -> no heading, no fabrication
        self.assertNotIn("Next Steps", md)

    def test_composed_doc_skipped_when_no_source_phase_exists(self):
        audit_phases = [("recon", "Recon"), ("report", "Report")]
        self.assertIsNone(docs.render_composed_doc(
            "Demo", "Product Requirements (PRD)", docs.PRD_SOURCES,
            audit_phases, {}))

    def test_qa_report_includes_verify_summary_or_na(self):
        md = docs.render_qa_report("Demo", self.phases, self.outputs,
                                   verify_summary="VERIFIED (compiled cleanly)")
        self.assertIn("Go, with caveats.", md)
        self.assertIn("VERIFIED (compiled cleanly)", md)
        md2 = docs.render_qa_report("Demo", self.phases, self.outputs, "")
        self.assertIn("N/A — no structured verification result exists", md2)

    def test_known_limitations_lists_honest_gaps(self):
        consensus = dict(self.consensus)
        consensus["tech_specs"] = False
        finding = {"source": "secret_scan", "category": "secret_hardcoded",
                   "severity": "Critical", "title": "Hardcoded secret (aws_key) at a.swift:3",
                   "status": "open"}
        md = docs.render_known_limitations(
            "Demo", self.phases, self.outputs, consensus,
            verify_summary="FAILED (2 errors)", findings=[finding],
            blocked_conflict={"detail": "lane primary_ui vs data_domain on App.swift"})
        self.assertIn("**App Features** did not run", md)
        self.assertIn("**Tech Specs** completed without consensus", md)
        self.assertIn("Build verification did not pass: FAILED (2 errors).", md)
        self.assertIn("Hardcoded secret (aws_key) at a.swift:3", md)
        self.assertIn("lane primary_ui vs data_domain on App.swift", md)

    def test_known_limitations_clean_run(self):
        outputs = dict(self.outputs)
        outputs["app_features"] = "features"
        outputs["project_plan"] = "plan"
        consensus = {k: True for k in outputs}
        md = docs.render_known_limitations("Demo", self.phases, outputs, consensus,
                                           verify_summary="VERIFIED (ok)",
                                           findings=[], blocked_conflict=None)
        self.assertIn("None recorded", md)

    def test_write_project_docs_emits_full_doc_set(self):
        written = docs.write_project_docs(
            self.app_dir, "Demo", self.phases, self.outputs,
            consensus_status=self.consensus, verify_summary="", findings=[])
        for rel in ("docs/PRD.md", "docs/TECHNICAL_ARCHITECTURE.md",
                    "docs/QA_REPORT.md", "docs/KNOWN_LIMITATIONS.md",
                    "docs/PROJECT_DOCUMENTATION.md", "docs/LAUNCH_READINESS.md"):
            self.assertIn(rel, written)
            self.assertTrue(os.path.exists(os.path.join(self.app_dir, rel)))

    def test_write_project_docs_skips_app_docs_for_audit_workflow(self):
        audit_phases = [("recon", "Recon"), ("security", "Security"),
                        ("report", "Report")]
        written = docs.write_project_docs(
            self.app_dir, "Demo", audit_phases, {"recon": "mapped"},
            consensus_status={}, workflow_name="audit")
        self.assertNotIn("docs/PRD.md", written)
        self.assertNotIn("docs/TECHNICAL_ARCHITECTURE.md", written)
        self.assertNotIn("docs/QA_REPORT.md", written)
        self.assertIn("docs/KNOWN_LIMITATIONS.md", written)
        self.assertIn("docs/LAUNCH_READINESS.md", written)
        self.assertFalse(os.path.exists(os.path.join(self.app_dir, "docs", "PRD.md")))


class TestDocMap(unittest.TestCase):
    """V3 board 5.1: docs' source tuples are externalized to
    sections/documentation/doc_map.json — the built-in default renders
    byte-identically, disk wins, a corrupt file falls back loudly."""
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "doc_render_frozen.json")

    def _keel(self):
        with open(self.FIXTURE, encoding="utf-8") as fh:
            return json.load(fh)

    def _render_via_map(self, dm, keel):
        ordered = [tuple(x) for x in keel["ordered"]]
        po, vsum = keel["phase_outputs"], keel["verify_summary"]
        out = {}
        for e in dm["docs"]:
            if e.get("kind") == "qa":
                md = docs.render_qa_report("TeaTimer", ordered, po, vsum,
                                           sources=e["sources"], intro=e["intro"],
                                           doc_title=e["title"])
            else:
                md = docs.render_composed_doc("TeaTimer", e["title"], e["sources"],
                                              ordered, po, intro=e["intro"])
            out[e["filename"]] = md
        return out

    def test_default_map_render_is_byte_identical_to_frozen(self):
        # The keel: a single-char change to any source tuple or intro breaks this.
        keel = self._keel()
        rendered = self._render_via_map(docs._default_doc_map(), keel)
        for fn in ("PRD.md", "TECHNICAL_ARCHITECTURE.md", "QA_REPORT.md"):
            self.assertEqual(rendered[fn], keel["cases"][fn], "byte drift in " + fn)

    def test_committed_doc_map_equals_builtin_default(self):
        # The shipped file must be the verbatim dump of _default_doc_map() — edit
        # a source tuple without regenerating and this fails (single source truth).
        path = os.path.join(self.HERE, "sections", "documentation", "doc_map.json")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(),
                             json.dumps(docs._default_doc_map(), indent=2))

    def test_absent_map_is_seeded_then_disk_wins_silently(self):
        tmp = tempfile.mkdtemp()
        warns = []
        dm = docs.load_doc_map(tmp, on_warn=warns.append)
        seeded = os.path.join(tmp, "sections", "documentation", "doc_map.json")
        self.assertTrue(os.path.exists(seeded), "absent → seed to disk")
        self.assertEqual(dm, docs._default_doc_map())
        with open(seeded, encoding="utf-8") as fh:
            self.assertEqual(fh.read(),
                             json.dumps(docs._default_doc_map(), indent=2))
        self.assertFalse(warns, "a healthy seed banners nothing")

    def test_disk_map_overrides_sources_without_code_change(self):
        tmp = tempfile.mkdtemp()
        docs.load_doc_map(tmp)  # seed
        seeded = os.path.join(tmp, "sections", "documentation", "doc_map.json")
        data = json.loads(open(seeded, encoding="utf-8").read())
        data["docs"][0]["sources"] = ["app_features"]
        with open(seeded, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        self.assertEqual(docs.load_doc_map(tmp)["docs"][0]["sources"],
                         ["app_features"], "on-disk edit must win")

    def test_corrupt_map_falls_back_loudly_and_is_not_clobbered(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "sections", "documentation")
        os.makedirs(d)
        with open(os.path.join(d, "doc_map.json"), "w") as fh:
            fh.write("{ not json")
        warns = []
        dm = docs.load_doc_map(tmp, on_warn=warns.append)
        self.assertEqual(dm, docs._default_doc_map(), "corrupt → built-in default")
        self.assertTrue(warns and "unreadable" in warns[0], "must fire on_warn")
        with open(os.path.join(d, "doc_map.json")) as fh:
            self.assertEqual(fh.read(), "{ not json",
                             "corrupt user file must NOT be clobbered")

    def test_structurally_invalid_map_falls_back_loudly(self):
        tmp = tempfile.mkdtemp()
        d = os.path.join(tmp, "sections", "documentation")
        os.makedirs(d)
        # valid JSON, but a doc entry is missing its filename
        with open(os.path.join(d, "doc_map.json"), "w") as fh:
            fh.write(json.dumps({"docs": [{"title": "x", "sources": []}]}))
        warns = []
        dm = docs.load_doc_map(tmp, on_warn=warns.append)
        self.assertEqual(dm, docs._default_doc_map())
        self.assertTrue(warns, "malformed entry must fire on_warn")

    def test_blueprint_ships_40_slots_11_categories_owned(self):
        dm = docs._default_doc_map()
        self.assertEqual(len(dm["categories"]), 11)
        self.assertEqual(len(dm["slots"]), 40)
        cat_ids = {c["category_id"] for c in dm["categories"]}
        self.assertEqual(len({s["slot_id"] for s in dm["slots"]}), 40,
                         "slot ids unique")
        for s in dm["slots"]:
            self.assertIn(s["category"], cat_ids, "no orphan slot category")
            # 5.3 populates owner_section (= the slot's category); sources[] and
            # min_chars stay inert until a doc_map edit / 5.4.
            self.assertEqual(s["owner_section"], s["category"],
                             "5.3: every slot owned by its category-section")
            self.assertIn(s["owner_section"], cat_ids)
            self.assertEqual((s["sources"], s["min_chars"]), ([], None))

    def test_write_project_docs_honors_disk_map_and_banners_on_corrupt(self):
        # End-to-end through write_project_docs + the orch_dir/on_warn wiring.
        tmp = tempfile.mkdtemp()
        app_dir = tempfile.mkdtemp()
        docs.load_doc_map(tmp)  # seed
        seeded = os.path.join(tmp, "sections", "documentation", "doc_map.json")
        with open(seeded, "w") as fh:
            fh.write("{ corrupt")
        phases = [("final_review", "Final Review")]
        warns = []
        written = docs.write_project_docs(
            app_dir, "Demo", phases, {"final_review": "Ship it."},
            verify_summary="VERIFIED", orch_dir=tmp, on_warn=warns.append)
        # QA renders from the built-in fallback (final_review is a QA source)…
        self.assertIn("docs/QA_REPORT.md", written)
        # …and the fallback announced itself (R2: the interface must not lie).
        self.assertTrue(warns, "corrupt doc_map must banner through write path")

    def test_docs_module_stays_stdlib_only(self):
        # docs.py must not pull in orchestrator/evlib (import-light contract).
        import docs as _d
        src = open(_d.__file__, encoding="utf-8").read()
        for banned in ("import orchestrator", "import events", "import evlib"):
            self.assertNotIn(banned, src, banned + " leaked into docs.py")


if __name__ == "__main__":
    unittest.main()
