"""Enrollment E3: exact provenance tags and fabricated-citation detection."""

import json
import os
import shutil
import tempfile
import unittest

import artifacts
import docs
import docslint
import orchestrator as orch
import workflows


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProvenanceLintTests(unittest.TestCase):
    def setUp(self):
        self.target = tempfile.mkdtemp()
        self.app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.target, True)
        self.addCleanup(shutil.rmtree, self.app, True)
        os.makedirs(os.path.join(self.target, "docs"))
        with open(os.path.join(self.target, "src.py"), "w", encoding="utf-8") as fh:
            fh.write("print('observed')\n")
        with open(os.path.join(self.target, "docs", "README.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("Their claim.\n")

    def test_all_four_exact_tags_pass(self):
        text = (
            "The entry point is Python. [VERIFIED: src.py]\n\n"
            "The maintainers describe it. [FROM-THEIR-DOCS: docs/README.md]\n\n"
            "The deployment topology is unknown. [UNVERIFIED]\n\n"
            "A named external source informed this note. [RESEARCH: RFC 9110]\n")
        report = docslint.lint_text(text, self.target)
        self.assertEqual(report["paragraphs_checked"], 4)
        self.assertEqual(report["violations"], [])

    def test_untagged_paragraph_is_flagged_not_deleted(self):
        text = "This is an unsupported factual claim.\n"
        report = docslint.lint_text(text, self.target)
        self.assertEqual([v["kind"] for v in report["violations"]],
                         ["untagged_paragraph"])
        # The linter reports only; it has no rewritten-content field and leaves
        # its input unchanged.
        self.assertEqual(text, "This is an unsupported factual claim.\n")

    def test_heading_does_not_exempt_following_prose(self):
        report = docslint.lint_text(
            "## Architecture\nThis untagged claim follows the heading directly.\n",
            self.target)
        self.assertEqual(report["paragraphs_checked"], 1)
        self.assertEqual(report["violations"][0]["kind"],
                         "untagged_paragraph")

    def test_nonexistent_target_path_is_a_fabricated_citation(self):
        # SABOTAGE TEST: changing _existing_target_file to accept a missing
        # path must make this fail.  A syntactically valid tag is not evidence.
        report = docslint.lint_text(
            "There is a payment service. [VERIFIED: src/payments.py]",
            self.target)
        self.assertEqual(report["violation_count"], 1)
        finding = report["violations"][0]
        self.assertEqual(finding["kind"], "fabricated_citation")
        self.assertEqual(finding["path"], "src/payments.py")

    def test_traversal_and_symlink_escape_are_fabricated(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        self.addCleanup(lambda: os.path.exists(outside.name) and os.unlink(outside.name))
        os.symlink(outside.name, os.path.join(self.target, "escape"))
        report = docslint.lint_text(
            "A. [VERIFIED: ../outside]\n\nB. [VERIFIED: escape]", self.target)
        self.assertEqual([v["kind"] for v in report["violations"]],
                         ["fabricated_citation", "fabricated_citation"])

    def test_persisted_violations_feed_gap_report(self):
        report = docslint.lint_text("Unsupported claim.", self.target)
        docslint.write_report(self.app, report)
        docs.write_project_docs(self.app, "Adopted", [], {}, orch_dir=HERE,
                                artifact_reader=artifacts)
        with open(os.path.join(self.app, "docs", "GAP_REPORT.md"),
                  encoding="utf-8") as fh:
            rendered = fh.read()
        self.assertIn("Documentation provenance", rendered)
        self.assertIn("untagged_paragraph", rendered)
        self.assertIn("content was not deleted", rendered)

    def test_provenance_gap_prevents_false_complete_copy(self):
        report = docslint.lint_text("Unsupported claim.", self.target)
        rendered = docs.render_gap_report("Adopted", [], report)
        self.assertIn("handoff is not yet clean", rendered)
        self.assertNotIn("Handoff is complete", rendered)

    def test_doc_rebuild_hook_persists_report(self):
        phase = workflows.Phase("doc_rebuild", ".", "doc_rebuild.md", "docs")
        orch._hook_document_provenance(
            {"_workflow_target": "enroll", "_target_path": self.target},
            "adopted", self.app, phase, {}, key="doc_rebuild", md_path=None,
            transcript="", final_output="Claim. [UNVERIFIED]", coord=None,
            active=[], is_build=False, is_verify_repair=False,
            allow_writes=False, _needs_vlabel=False)
        with open(os.path.join(self.app, docslint.REPORT_RELATIVE_PATH),
                  encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["violation_count"], 0)

    def test_workflow_prompt_names_only_the_exact_contract(self):
        workflow = workflows.load_workflow("enroll", HERE)
        purpose = next(p.purpose for p in workflow.phases
                       if p.key == "doc_rebuild")
        for tag in ("[VERIFIED: <repo-relative-path>]",
                    "[FROM-THEIR-DOCS: <file>]", "[UNVERIFIED]",
                    "[RESEARCH: <source-url-or-name>]"):
            self.assertIn(tag, purpose)
        self.assertIn("Every factual paragraph", purpose)


if __name__ == "__main__":
    unittest.main()
