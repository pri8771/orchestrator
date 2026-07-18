"""V3 board 5.0: the Documentation section as pure data.

sections/documentation/{section,roles,contracts,routing}.json runs
end-to-end through the 3.1 loader with zero core-code changes: the section
loads clean, its workflow resolves to a real 2-phase workflow, its cast has
no dangling role refs, the gap-fill contract round-trips through
schemas.extract_structured_blocks, docs._phase_section ingests the fenced
output, and a corrupted manifest surfaces a visible banner (never silent).
"""
import json
import os
import shutil
import tempfile
import unittest

import docs
import orchestrator as orch
import schemas
import sections as seclib

HERE = orch.HERE
DOC_DIR = os.path.join(HERE, "sections", "documentation")


class TestManifestsParse(unittest.TestCase):
    def test_exactly_the_shipped_manifests_exist_and_parse(self):
        # 5.0 shipped four manifests; 5.1 added doc_map.json (the doc blueprint).
        names = sorted(n for n in os.listdir(DOC_DIR) if n.endswith(".json"))
        self.assertEqual(names, ["contracts.json", "doc_map.json", "roles.json",
                                 "routing.json", "section.json"])
        for n in names:
            with open(os.path.join(DOC_DIR, n), encoding="utf-8") as fh:
                json.load(fh)   # each is valid JSON

    def test_section_loads_clean_with_no_banner(self):
        # A healthy load emits ZERO config_fallback events.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        sec = seclib.load_section("documentation", HERE, app_dir=tmp)
        self.assertEqual((sec.id, sec.title, sec.workflow_name),
                         ("documentation", "Documentation", "documentation"))
        self.assertEqual(sec.artifact_types_emitted, ["gap", "reconcile"])
        self.assertFalse(os.path.exists(os.path.join(tmp, "events.jsonl")),
                         "a healthy section load banners nothing")

    def test_emitted_types_are_registered(self):
        # §16 / the registry consistency rule: every emitted type must exist.
        import artifacts as artlib
        for t in seclib.load_section(
                "documentation", HERE).artifact_types_emitted:
            self.assertIn(t, artlib.SEED_TYPES, t)


class TestWorkflowAndCast(unittest.TestCase):
    def test_workflow_resolves_to_the_two_phase_documentation_workflow(self):
        wf = seclib.load_section("documentation", HERE).workflow
        self.assertEqual(wf.name, "documentation")
        self.assertEqual([p.key for p in wf.phases],
                         ["doc_gap_fill", "doc_coherence"],
                         "resolved the real workflow, not the app_build "
                         "fallback")

    def test_no_dangling_role_references(self):
        wf = seclib.load_section("documentation", HERE).workflow
        with open(os.path.join(DOC_DIR, "roles.json"), encoding="utf-8") as fh:
            declared = {r["id"] for r in json.load(fh)["roles"]}
        cast = set()
        for p in wf.phases:
            cast |= set(p.roles)
        self.assertTrue(cast <= declared,
                        "phase-cast roles %s not all in %s"
                        % (sorted(cast), sorted(declared)))
        self.assertTrue(cast, "the workflow must actually cast its roles")


class TestContractAndIngestion(unittest.TestCase):
    def test_gap_fill_contract_uses_phase_output_json(self):
        contracts = seclib.load_contracts(HERE, section="documentation")
        gap = [c for c in contracts if c.get("phase_key") == "doc_gap_fill"]
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0]["fence_tag"], "phase-output-json")
        self.assertEqual(gap[0]["required_fields"], "phase_structured_output")
        self.assertEqual(schemas.REQUIRED_FIELDS["phase_structured_output"],
                         ["phase", "doc_sections"])

    def test_contract_round_trips_and_docs_ingests_it(self):
        # The end-to-end gate: a doc_gap_fill wrap-up carrying the contract's
        # fenced block is ingested by docs' existing _phase_section path.
        out = ('Prose.\n\n```phase-output-json\n'
               '{"phase": "doc_gap_fill", "doc_sections": '
               '{"overview": "# Overview\\n\\nThe app does X."}}\n```\n')
        blocks = schemas.extract_structured_blocks(
            out, "phase-output-json", required_fields=["phase", "doc_sections"])
        self.assertEqual(len(blocks), 1)
        rendered = docs._phase_section("Documentation Gap Fill", out)
        self.assertIn("The app does X.", rendered,
                      "docs._phase_section ingested the keyed doc_sections")


class TestCorruptFallsBackLoudly(unittest.TestCase):
    def _events(self, app_dir):
        p = os.path.join(app_dir, "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def _seed(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        d = os.path.join(tmp, "sections", "documentation")
        os.makedirs(d)
        for n in ("section.json", "roles.json", "contracts.json",
                  "routing.json"):
            shutil.copy(os.path.join(DOC_DIR, n), os.path.join(d, n))
        return tmp, d

    def test_corrupt_section_json_banners_not_silent(self):
        tmp, d = self._seed()
        with open(os.path.join(d, "section.json"), "w") as fh:
            fh.write("{corrupt")
        app = os.path.join(tmp, "app")
        os.makedirs(app)
        sec = seclib.load_section("documentation", tmp, app_dir=app)
        self.assertIsNotNone(sec, "corrupt manifest must not crash")
        kinds = [e.get("kind") for e in self._events(app)]
        self.assertIn("config_fallback", kinds,
                      "a corrupt section.json must surface a banner")

    def test_corrupt_contracts_json_banners_not_silent(self):
        tmp, d = self._seed()
        with open(os.path.join(d, "contracts.json"), "w") as fh:
            fh.write("{not valid")
        app = os.path.join(tmp, "app")
        os.makedirs(app)
        seclib.load_contracts(tmp, section="documentation", app_dir=app)
        kinds = [e.get("kind") for e in self._events(app)]
        self.assertIn("config_fallback", kinds,
                      "a corrupt contracts.json must surface a banner")


if __name__ == "__main__":
    unittest.main()
