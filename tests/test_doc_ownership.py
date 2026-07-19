"""V3 board 5.3: doc-slot ownership + cross-lineage conflict → gap artifacts.

Every blueprint slot has one owning section (owner_section = its category). When
two `final` artifacts from DISJOINT lineages claim one slot, the render leaves it
unfilled behind an explicit conflict note (never last-write-wins) and emits
exactly ONE idempotent `gap` artifact (reason 'lineage_conflict'); re-rendering
the same contention publishes nothing new; resolving it (supersede-to-drop-claim)
lets the slot render normally. docs.py stays a stdlib leaf — the store is the
injected reader; tests import artifacts only to build fixtures and publish gaps.
"""
import json
import os
import shutil
import tempfile
import unittest

import artifacts as artlib
import docs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OwnershipBase(unittest.TestCase):
    def setUp(self):
        # app_dir IS the artifact project_dir on the real orchestrator path.
        self.app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.app, True)
        self.reg = artlib.load_registry(HERE)
        self.warns = []

    def pub(self, title, slot, body, supersedes=None, type="idea"):
        meta = {"type": type, "title": title, "doc_slots": [slot]}
        return artlib.publish(self.app, body, meta, self.reg, consensus=True,
                              supersedes=supersedes)

    def write_docs(self, reader=None):
        return docs.write_project_docs(
            self.app, "Demo", [], {}, orch_dir=HERE,
            on_warn=self.warns.append,
            artifact_reader=reader if reader is not None else artlib)

    def gaps(self):
        return artlib.list_artifacts(self.app, type="gap")

    def blueprint(self):
        p = os.path.join(self.app, "docs", "HANDOFF_BLUEPRINT.md")
        with open(p, encoding="utf-8") as fh:
            return fh.read() if os.path.exists(p) else ""


class TestConflictAndGap(OwnershipBase):
    def test_disjoint_lineages_conflict_note_and_exactly_one_gap(self):
        # AC2: both ids named, neither body rendered, slot unfilled, ONE gap.
        a = self.pub("A", "problem_statement", "BODY A")
        b = self.pub("B", "problem_statement", "BODY B")
        self.write_docs()
        bp = self.blueprint()
        self.assertIn("cross-lineage ownership conflict", bp)
        self.assertIn(a, bp)
        self.assertIn(b, bp)
        self.assertNotIn("BODY A", bp)
        self.assertNotIn("BODY B", bp)
        gaps = self.gaps()
        self.assertEqual(len(gaps), 1)
        f = gaps[0]["fields"]
        self.assertEqual(f["reason"], "lineage_conflict")
        self.assertEqual(f["slot_id"], "problem_statement")
        self.assertEqual(f["owner_section"], "ideas")
        self.assertEqual(f["contending"], sorted([a, b]))
        self.assertEqual(gaps[0]["status"], "final")
        self.assertEqual(gaps[0]["doc_slots"], [], "gap must not self-ingest")


class TestMultiLineage(OwnershipBase):
    def test_three_lineage_conflict_note_is_count_aware(self):
        # The note must not hardcode 2-way "both"/"Neither" for N>=3 (R2).
        a = self.pub("A", "problem_statement", "BODY A")
        b = self.pub("B", "problem_statement", "BODY B")
        c = self.pub("C", "problem_statement", "BODY C")
        self.write_docs()
        bp = self.blueprint()
        for x in (a, b, c):
            self.assertIn(x, bp)
        self.assertIn("3 artifacts from disjoint lineages", bp)
        self.assertNotIn("both claim this slot", bp)
        self.assertNotIn("Neither supersedes", bp)
        self.assertEqual(len(self.gaps()), 1, "still one gap for the conflict")

    def test_corrupt_existing_gap_fields_does_not_abort_emission(self):
        # A prior gap with a non-dict 'fields' must not crash the open-gap scan;
        # a real conflict still emits its gap (never-raises §13.3).
        self.pub("A", "problem_statement", "BODY A")
        self.pub("B", "problem_statement", "BODY B")
        gid = artlib.publish(self.app, "junk",
                             {"type": "gap", "title": "J", "impact": "x"},
                             self.reg, consensus=True)
        mp = os.path.join(artlib.artifacts_root(self.app), gid, "meta.json")
        with open(mp, encoding="utf-8") as fh:
            m = json.load(fh)
        m["fields"] = ["lineage_conflict"]          # corrupt: fields is a list
        with open(mp, "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        self.write_docs()                            # must not raise/abort
        self.assertFalse(any("render skipped" in w for w in self.warns),
                         "the corrupt gap must not abort the emit step")
        reasons = [g["fields"].get("reason") for g in self.gaps()
                   if isinstance(g.get("fields"), dict)]
        self.assertIn("lineage_conflict", reasons,
                      "the real conflict still emitted its gap")


class TestIdempotency(OwnershipBase):
    def test_triple_rerender_publishes_zero_additional_gaps(self):
        # AC3: deterministic dedupe key → re-render never multiplies gaps.
        self.pub("A", "problem_statement", "BODY A")
        self.pub("B", "problem_statement", "BODY B")
        keys = set()
        for _ in range(3):
            self.write_docs()
            gaps = self.gaps()
            self.assertEqual(len(gaps), 1, "idempotent across re-renders")
            keys.add(gaps[0]["fields"]["dedupe_key"])
        self.assertEqual(len(keys), 1, "dedupe key stable across renders")

    def test_changed_contending_set_emits_a_fresh_gap(self):
        # AC3: a genuinely different conflict (a 3rd claimant) is a new key.
        self.pub("A", "problem_statement", "BODY A")
        self.pub("B", "problem_statement", "BODY B")
        self.write_docs()
        self.assertEqual(len(self.gaps()), 1)
        self.pub("C", "problem_statement", "BODY C")   # third disjoint claimant
        self.write_docs()
        self.assertEqual(len(self.gaps()), 2, "new contending set → fresh gap")


class TestResolution(OwnershipBase):
    def test_supersede_drops_claim_resolves_slot_and_adds_no_gap(self):
        # AC3: superseding one tip so it drops the tag lets the other fill.
        a = self.pub("A", "problem_statement", "BODY A")
        b = self.pub("B", "problem_statement", "BODY B")
        self.write_docs()
        self.assertEqual(len(self.gaps()), 1)
        # A2 supersedes A but drops the slot tag → A's lineage no longer claims.
        artlib.publish(self.app, "A2 body",
                       {"type": "idea", "title": "A", "doc_slots": []},
                       self.reg, consensus=True, supersedes=a)
        self.write_docs()
        bp = self.blueprint()
        self.assertIn("BODY B", bp)
        self.assertIn("_Source: artifact %s" % b, bp)
        self.assertEqual(len(self.gaps()), 1, "resolution adds no new gap")


class TestNeverLastWriteWins(OwnershipBase):
    def test_neither_body_chosen_and_key_is_content_not_order(self):
        # AC4: publish order / mtime never decides; the key is content-derived.
        a = self.pub("A", "problem_statement", "BODY A")
        b = self.pub("B", "problem_statement", "BODY B")
        self.write_docs()
        bp = self.blueprint()
        self.assertNotIn("BODY A", bp)
        self.assertNotIn("BODY B", bp)
        ma = artlib.load_meta(self.app, a)
        mb = artlib.load_meta(self.app, b)
        expected = docs._conflict_dedupe_key(
            "problem_statement", [ma["content_hash"], mb["content_hash"]])
        self.assertEqual(self.gaps()[0]["fields"]["dedupe_key"], expected)


class TestOwnershipLint(OwnershipBase):
    def test_load_doc_map_lints_unowned_and_unknown_owner(self):
        # AC1: unowned / unknown-owner slots banner through on_warn.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        d = os.path.join(tmp, "sections", "documentation")
        os.makedirs(d)
        m = docs._default_doc_map()
        m["slots"][0]["owner_section"] = None
        m["slots"][1]["owner_section"] = "bogus_section"
        with open(os.path.join(d, "doc_map.json"), "w", encoding="utf-8") as fh:
            json.dump(m, fh)
        warns = []
        docs.load_doc_map(tmp, on_warn=warns.append)
        self.assertTrue(any("no owner_section" in w for w in warns))
        self.assertTrue(any("unknown owner_section" in w and "bogus_section" in w
                            for w in warns))

    def test_healthy_default_map_lints_nothing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        docs.load_doc_map(tmp)          # seeds the valid default
        warns = []
        docs.load_doc_map(tmp, on_warn=warns.append)   # re-load, disk-wins
        self.assertFalse(warns, "a fully-owned default map banners nothing")

    def test_unowned_contended_slot_renders_note_but_emits_no_gap(self):
        # AC1: an unowned slot never picks a winner, but has no owner to attribute
        # a gap to — render the note, record no contention.
        a = self.pub("A", "slot_u", "BODY A")
        b = self.pub("B", "slot_u", "BODY B")
        self.assertTrue(a and b)
        dm = {"schema_version": 1, "docs": [],
              "categories": [{"category_id": "ideas", "title": "Ideas"}],
              "slots": [{"slot_id": "slot_u", "category": "ideas", "title": "U",
                         "sources": [], "owner_section": None,
                         "min_chars": None}]}
        contentions = []
        out = docs.render_handoff_blueprint(
            "Demo", dm, [], {}, self.app, artlib,
            on_warn=self.warns.append, contentions=contentions)
        self.assertIn("no valid owner_section", out)
        self.assertEqual(contentions, [], "unowned slot emits no gap")


class TestGapPublishFailure(OwnershipBase):
    def test_publish_failure_degrades_to_note_plus_warning(self):
        # AC7: a gap-publish failure never fails the render.
        self.pub("A", "problem_statement", "BODY A")
        self.pub("B", "problem_statement", "BODY B")

        class FailPub:
            def __getattr__(self, n):
                return getattr(artlib, n)   # delegate all reads to the real store

            def publish(self, *a, **k):
                return None                 # every gap publish "fails"

        written = self.write_docs(reader=FailPub())
        self.assertIn("docs/HANDOFF_BLUEPRINT.md", written, "render survived")
        self.assertIn("cross-lineage", self.blueprint())
        self.assertTrue(any("gap publish FAILED" in w for w in self.warns))
        self.assertEqual(len(self.gaps()), 0, "nothing published on failure")


class TestNoFeedbackLoop(OwnershipBase):
    def test_gap_type_registered_with_required_fields(self):
        # AC6 / subtask 6: the gap type exists with title/body/impact required.
        reg = artlib.load_registry(HERE)
        self.assertEqual(reg["types"]["gap"]["required"],
                         ["title", "body", "impact"])

    def test_emitted_gap_never_matches_a_slot(self):
        # Q-E: the gap (doc_slots=[]) can never be ingested or re-conflict.
        self.pub("A", "problem_statement", "BODY A")
        self.pub("B", "problem_statement", "BODY B")
        self.write_docs()
        self.write_docs()               # re-render after the gap exists
        self.assertEqual(len(self.gaps()), 1, "gap did not self-multiply")
        self.assertNotIn("Doc-slot conflict", self.blueprint(),
                         "gap body/title never ingested into a slot")


if __name__ == "__main__":
    unittest.main()
