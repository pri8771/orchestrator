"""V3 board 5.2: SUBSCRIBE-mode artifact ingestion into docs' blueprint slots.

docs.render_handoff_blueprint passively ingests published `final` artifacts from
the artifact store per doc_map slot mappings — a doc renders from artifacts
ALONE, with per-slot precedence artifact > phase-output > empty (never merged),
a provenance line naming each artifact source, an explicit conflict note (never a
silent branch-pick) on a branched lineage, a visible-unfilled marker otherwise,
and byte-identical 5.1 output when no reader is supplied. docs.py stays
stdlib-only (the store is an INJECTED reader), so tests import artifacts only to
BUILD fixtures.
"""
import json
import os
import shutil
import stat
import tempfile
import unittest

import artifacts as artlib
import docs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FILL_RE = "_Source: artifact %s v%s_"
_UNFILLED = "_Unfilled — no artifact or phase output for this slot._"


def _cat(cid, title):
    return {"category_id": cid, "title": title}


def _slot(sid, cat, title, sources=None, artifact=None):
    s = {"slot_id": sid, "category": cat, "title": title,
         "sources": sources or [], "owner_section": None, "min_chars": None}
    if artifact is not None:
        s["artifact"] = artifact
    return s


class SubscribeBase(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.proj, True)
        self.reg = artlib.load_registry(HERE)
        self.warns = []

    def pub(self, title, slot, body, consensus=True, supersedes=None,
            status=None, type="idea"):
        meta = {"type": type, "title": title, "doc_slots": [slot]}
        if status:
            meta["status"] = status
        return artlib.publish(self.proj, body, meta, self.reg,
                              consensus=consensus, supersedes=supersedes)

    def render(self, doc_map, phase_outputs=None):
        return docs.render_handoff_blueprint(
            "Demo", doc_map, [], phase_outputs or {}, self.proj, artlib,
            on_warn=self.warns.append)

    def _map(self, slots, cats=None):
        return {"schema_version": 1, "docs": [],
                "categories": cats or [_cat("ideas", "Ideas")], "slots": slots}


class TestArtifactsAlone(SubscribeBase):
    def test_renders_from_artifacts_alone_with_provenance(self):
        # AC1 (the plan's named gate): zero phase_outputs, slots fill from
        # artifacts, each carrying a provenance line naming id + version.
        a1 = self.pub("PS", "problem_statement", "# Problem\n\nCan't focus.")
        a2 = self.pub("ML", "market_landscape", "# Market\n\nShallow.")
        dm = self._map([_slot("problem_statement", "ideas", "Problem Statement"),
                        _slot("market_landscape", "ideas", "Market Landscape")])
        out = self.render(dm)
        self.assertIn("Can't focus.", out)
        self.assertIn("Shallow.", out)
        self.assertIn(_FILL_RE % (a1, 1), out)
        self.assertIn(_FILL_RE % (a2, 1), out)


class TestFinalOnly(SubscribeBase):
    def test_non_final_artifacts_never_ingested(self):
        # AC2: a draft and a never-finalized pending_review must NOT appear.
        self.pub("D", "problem_statement", "DRAFT BODY", status="draft",
                 consensus=False)
        # spec_bundle policy = requires_human → publish lands pending_review.
        self.pub("P", "problem_statement", "PENDING BODY", type="spec_bundle",
                 consensus=True)
        dm = self._map([_slot("problem_statement", "ideas", "Problem Statement")])
        out = self.render(dm)
        self.assertNotIn("DRAFT BODY", out)
        self.assertNotIn("PENDING BODY", out)
        self.assertIn(_UNFILLED, out)

    def test_superseded_version_never_renders_over_latest(self):
        # §12.2 bus analog: v1 tagged the slot, v2 supersedes it (also tagged);
        # only v2 (the tip) renders, never v1.
        v1 = self.pub("S", "problem_statement", "OLD v1 body")
        v2 = artlib.publish(self.proj, "NEW v2 body",
                            {"type": "idea", "title": "S",
                             "doc_slots": ["problem_statement"]},
                            self.reg, consensus=True, supersedes=v1)
        dm = self._map([_slot("problem_statement", "ideas", "Problem Statement")])
        out = self.render(dm)
        self.assertIn("NEW v2 body", out)
        self.assertNotIn("OLD v1 body", out)
        self.assertIn(_FILL_RE % (v2, 2), out)


class TestTypeFilter(SubscribeBase):
    def test_type_filter_judges_the_resolved_tip_not_an_ancestor(self):
        # A supersede can change an artifact's type; a types-restricted slot must
        # judge the TIP's type, not an ancestor that happened to match.
        v1 = artlib.publish(self.proj, "RB BODY",
                            {"type": "research_brief", "title": "R",
                             "doc_slots": ["slot_x"], "sources": ["http://x"]},
                            self.reg, consensus=True)
        artlib.publish(self.proj, "IDEA TIP BODY",
                      {"type": "idea", "title": "R", "doc_slots": ["slot_x"]},
                      self.reg, consensus=True, supersedes=v1)
        dm = self._map([_slot("slot_x", "ideas", "Slot X",
                              artifact={"types": ["research_brief"]})])
        out = self.render(dm)
        self.assertNotIn("IDEA TIP BODY", out)   # tip is 'idea', not in types
        self.assertIn(_UNFILLED, out)


class TestPrecedence(SubscribeBase):
    def test_artifact_beats_phase_output_and_absence_falls_through(self):
        # AC3: artifact > phase-output > empty, never merged.
        a = self.pub("A", "slot_a", "ARTIFACT BODY")
        dm = self._map([
            _slot("slot_a", "ideas", "Slot A", sources=["tech_specs"]),
            _slot("slot_b", "ideas", "Slot B", sources=["tech_specs"]),
            _slot("slot_c", "ideas", "Slot C"),
        ])
        out = self.render(dm, phase_outputs={"tech_specs": "PHASE BODY"})
        # slot_a: artifact wins, phase output NOT merged in
        self.assertIn("ARTIFACT BODY", out)
        self.assertIn(_FILL_RE % (a, 1), out)
        a_sec = out.split("### Slot A")[1].split("### Slot B")[0]
        self.assertNotIn("PHASE BODY", a_sec)
        # slot_b: no artifact → phase output, and NO provenance line
        b_sec = out.split("### Slot B")[1].split("### Slot C")[0]
        self.assertIn("PHASE BODY", b_sec)
        self.assertNotIn("_Source: artifact", b_sec)
        # slot_c: neither → unfilled
        self.assertIn(_UNFILLED, out.split("### Slot C")[1])


class TestBranchedConflict(SubscribeBase):
    def test_branched_lineage_renders_conflict_not_a_pick(self):
        # AC4: two unreconciled heads → explicit conflict note, neither picked.
        v1 = self.pub("Spec", "api_contracts", "v1 body")
        c1 = artlib.publish(self.proj, "CHILD A body",
                           {"type": "idea", "title": "Spec",
                            "doc_slots": ["api_contracts"]},
                           self.reg, consensus=True, supersedes=v1)
        c2 = artlib.publish(self.proj, "CHILD B body",
                           {"type": "idea", "title": "Spec",
                            "doc_slots": ["api_contracts"]},
                           self.reg, consensus=True, supersedes=v1)
        self.assertTrue(c1 and c2)
        dm = self._map([_slot("api_contracts", "ideas", "API Contracts")])
        out = self.render(dm)   # must not raise
        self.assertIn("is branched across heads", out)
        self.assertNotIn("CHILD A body", out)
        self.assertNotIn("CHILD B body", out)


class TestDegradeOnMissing(SubscribeBase):
    def test_unreadable_body_degrades_with_warning(self):
        # AC6: a final artifact whose body.md vanished degrades to unfilled +
        # a visible warning, never a crash, never fabricated content.
        aid = self.pub("X", "problem_statement", "GONE SOON")
        body = os.path.join(artlib.artifacts_root(self.proj), aid, "body.md")
        os.remove(body)
        dm = self._map([_slot("problem_statement", "ideas", "Problem Statement")])
        out = self.render(dm)
        self.assertIn(_UNFILLED, out)
        self.assertTrue(any("unreadable" in w for w in self.warns))


class TestReaderNoneParity(unittest.TestCase):
    def test_reader_none_is_byte_identical_and_omits_blueprint(self):
        # AC5: with no reader the doc set is byte-identical to 5.1 and no
        # HANDOFF_BLUEPRINT.md is written; with a reader over an EMPTY store the
        # 5.1 docs are still byte-identical (blueprint is purely additive).
        appA = tempfile.mkdtemp(); appB = tempfile.mkdtemp()
        empty_proj = tempfile.mkdtemp()
        for d in (appA, appB, empty_proj):
            self.addCleanup(shutil.rmtree, d, True)
        phases = [("final_review", "Final Review")]
        po = {"final_review": "Ship it."}
        wa = docs.write_project_docs(appA, "Demo", phases, po,
                                     verify_summary="VERIFIED", orch_dir=HERE,
                                     artifact_reader=None)
        # appB shares empty_proj as its store — but write_project_docs uses
        # app_dir as project_dir, so point appB at an empty store by using a
        # fresh app_dir that has no artifacts/ dir.
        docs.write_project_docs(appB, "Demo", phases, po,
                                verify_summary="VERIFIED", orch_dir=HERE,
                                artifact_reader=artlib)
        self.assertNotIn("docs/HANDOFF_BLUEPRINT.md", wa)
        for rel in ("docs/PRD.md", "docs/TECHNICAL_ARCHITECTURE.md",
                    "docs/QA_REPORT.md", "docs/PROJECT_DOCUMENTATION.md",
                    "docs/KNOWN_LIMITATIONS.md", "docs/LAUNCH_READINESS.md"):
            pa, pb = os.path.join(appA, rel), os.path.join(appB, rel)
            if os.path.exists(pa) or os.path.exists(pb):
                with open(pa, encoding="utf-8") as fa, \
                        open(pb, encoding="utf-8") as fb:
                    self.assertEqual(fa.read(), fb.read(), "byte drift in " + rel)


class TestImportPurity(unittest.TestCase):
    def test_docs_never_imports_artifacts(self):
        # AC7: docs.py importable without artifacts.py (leaf purity).
        with open(docs.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import artifacts", src)


class TestMixed(SubscribeBase):
    def test_artifact_phase_and_empty_slots_coexist(self):
        # AC8: one render with all three slot states present at once.
        a = self.pub("A", "slot_a", "FROM ARTIFACT")
        dm = self._map([
            _slot("slot_a", "ideas", "Slot A"),
            _slot("slot_b", "ideas", "Slot B", sources=["tech_specs"]),
            _slot("slot_c", "ideas", "Slot C"),
        ])
        out = self.render(dm, phase_outputs={"tech_specs": "FROM PHASE"})
        self.assertIn("FROM ARTIFACT", out)
        self.assertIn(_FILL_RE % (a, 1), out)
        self.assertIn("FROM PHASE", out)
        self.assertIn(_UNFILLED, out)


class TestMalformedMap(SubscribeBase):
    def test_non_list_slot_sources_degrades_visibly_not_crashes(self):
        # A disk-wins doc_map isn't validated at the slot level; a 'forgot the
        # brackets' sources typo must NOT silently drop (string char-iteration)
        # nor crash the whole blueprint (int → `for k in 5`).
        dm = self._map([
            _slot("s_str", "ideas", "S Str", sources="detailed_discussion"),
            _slot("s_int", "ideas", "S Int", sources=5),
            _slot("s_ok", "ideas", "S OK"),
        ])
        out = self.render(dm, phase_outputs={"detailed_discussion": "PHASE X"})
        self.assertIsNotNone(out, "a malformed slot must not abort the blueprint")
        self.assertNotIn("PHASE X", out)          # string didn't char-iterate to a hit
        self.assertIn("S OK", out)                # other slots still render
        self.assertTrue(any("not a list" in w for w in self.warns))

    def test_corrupt_doc_slots_on_tip_does_not_crash(self):
        # A supersede-tip whose meta.doc_slots is a non-list (hand corruption;
        # publish normalizes, so this only arises from a damaged file) must
        # degrade to unfilled, never raise `tag in 1`.
        v1 = self.pub("V1", "slot_x", "v1 body")
        v2 = artlib.publish(self.proj, "v2 body",
                           {"type": "idea", "title": "V1",
                            "doc_slots": ["slot_x"]},
                           self.reg, consensus=True, supersedes=v1)
        mp = os.path.join(artlib.artifacts_root(self.proj), v2, "meta.json")
        with open(mp, encoding="utf-8") as fh:
            m = json.load(fh)
        m["doc_slots"] = 1
        with open(mp, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        dm = self._map([_slot("slot_x", "ideas", "Slot X")])
        out = self.render(dm)                      # must not raise
        self.assertIsNotNone(out)
        self.assertIn(_UNFILLED, out)
        self.assertNotIn("v2 body", out)


class TestConcurrentSafety(SubscribeBase):
    def test_mid_flight_publish_dir_is_ignored(self):
        # A half-written .tmp-* artifact dir must never be ingested (atomic
        # publish: list_artifacts/lineage_index skip dot-prefixed entries).
        root = artlib.artifacts_root(self.proj)
        os.makedirs(os.path.join(root, ".tmp-halfwritten.123"))
        with open(os.path.join(root, ".tmp-halfwritten.123", "meta.json"),
                  "w") as fh:
            fh.write('{"type":"idea","title":"HALF",'
                     '"doc_slots":["problem_statement"],"status":"final"}')
        dm = self._map([_slot("problem_statement", "ideas", "Problem Statement")])
        out = self.render(dm)
        self.assertIn(_UNFILLED, out)
        self.assertNotIn("HALF", out)


if __name__ == "__main__":
    unittest.main()
