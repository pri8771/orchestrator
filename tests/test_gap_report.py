"""V3 board 5.4: gap report generation — empty/thin slots → structured gap
artifacts + docs/GAP_REPORT.md.

EMPTY = no final artifact matched AND no phase output. THIN = filled but body <
the slot's min_chars (seeded 200, per-slot overridable; exactly-min_chars is NOT
thin). Every render emits one idempotent gap per empty/thin slot (5.3 conflict
gaps share the report), addressed to the owning section; the report renders from
the CURRENT scan (never from the bus) so it can't surface stale gaps. docs.py
stays a stdlib leaf — the store is the injected reader; tests import artifacts
only to build fixtures.
"""
import json
import os
import shutil
import tempfile
import unittest

import artifacts as artlib
import docs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MC = docs.DEFAULT_MIN_CHARS   # 200


class GapBase(unittest.TestCase):
    def setUp(self):
        self.app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.app, True)
        self.reg = artlib.load_registry(HERE)
        self.warns = []

    def pub(self, title, slot, body, supersedes=None, type="idea"):
        return artlib.publish(self.app, body,
                              {"type": type, "title": title,
                               "doc_slots": [slot]},
                              self.reg, consensus=True, supersedes=supersedes)

    def cov(self, doc_map, phase_outputs=None):
        c = []
        docs.render_handoff_blueprint("D", doc_map, [], phase_outputs or {},
                                      self.app, artlib,
                                      on_warn=self.warns.append, coverage=c)
        return {r["slot_id"]: r for r in c}

    def write(self, reader=None, phase_outputs=None):
        return docs.write_project_docs(
            self.app, "D", [], phase_outputs or {}, orch_dir=HERE,
            on_warn=self.warns.append,
            artifact_reader=reader if reader is not None else artlib)

    def report(self):
        with open(os.path.join(self.app, "docs", "GAP_REPORT.md"),
                  encoding="utf-8") as fh:
            return fh.read()

    def gaps(self):
        return artlib.list_artifacts(self.app, type="gap")

    def _slot(self, sid, min_chars=MC, owner="ideas", sources=None):
        return {"slot_id": sid, "category": "ideas", "title": sid,
                "sources": sources or [], "owner_section": owner,
                "min_chars": min_chars}

    def _map(self, slots):
        return {"schema_version": 1, "docs": [],
                "categories": [{"category_id": "ideas", "title": "Ideas"}],
                "slots": slots}


class TestClassificationBoundary(GapBase):
    def test_exact_min_chars_is_filled_not_thin(self):
        # AC2: <min_chars thin; ==min_chars filled; >min_chars filled.
        self.pub("A", "s_lo", "x" * (MC - 1))
        self.pub("B", "s_eq", "x" * MC)
        self.pub("C", "s_hi", "x" * (MC + 1))
        c = self.cov(self._map([self._slot("s_lo"), self._slot("s_eq"),
                                self._slot("s_hi")]))
        self.assertEqual(c["s_lo"]["status"], "thin")
        self.assertEqual(c["s_eq"]["status"], "filled")
        self.assertEqual(c["s_hi"]["status"], "filled")

    def test_per_slot_override_and_null_fallback(self):
        # AC2: min_chars is per-slot data; null falls back to the code floor.
        self.pub("A", "s50", "x" * 60)     # 60 >= 50 → filled
        self.pub("B", "s200", "x" * 60)    # 60 < 200 → thin
        self.pub("C", "snull", "x" * 60)   # null → 200 → thin
        c = self.cov(self._map([self._slot("s50", 50), self._slot("s200", 200),
                                self._slot("snull", None)]))
        self.assertEqual(c["s50"]["status"], "filled")
        self.assertEqual(c["s200"]["status"], "thin")
        self.assertEqual(c["snull"]["status"], "thin")


class TestGapEmission(GapBase):
    def test_empty_thin_filled_produce_the_right_gaps(self):
        # AC1: filled → no gap; thin → thin gap; empty → empty gap.
        self.pub("PS", "problem_statement", "x" * 300)   # filled
        self.pub("TU", "target_user", "short")           # thin
        self.write()                                     # committed 40-slot map
        by_slot = {g["fields"]["slot_id"]: g["fields"]["reason"]
                   for g in self.gaps()}
        self.assertNotIn("problem_statement", by_slot, "filled → no gap")
        self.assertEqual(by_slot.get("target_user"), "thin")
        # the remaining 38 slots are empty
        self.assertEqual(sum(1 for r in by_slot.values() if r == "empty"), 38)
        self.assertEqual(sum(1 for r in by_slot.values() if r == "thin"), 1)

    def test_report_carries_all_three_reasons(self):
        # AC5: one report, three reasons (empty/thin/lineage_conflict).
        self.pub("PS", "problem_statement", "x" * 300)   # filled
        self.pub("TU", "target_user", "short")           # thin
        self.pub("cA", "value_proposition", "va")        # \
        self.pub("cB", "value_proposition", "vb")        # / disjoint → conflict
        self.write()
        reasons = {g["fields"]["reason"] for g in self.gaps()}
        self.assertEqual(reasons, {"empty", "thin", "lineage_conflict"})
        self.assertIn("Lineage conflicts: 1", self.report())

    def test_same_lineage_branch_classifies_as_empty_with_head_evidence(self):
        v1 = self.pub("S", "problem_statement", "v1")
        artlib.publish(self.app, "cA", {"type": "idea", "title": "S",
                                        "doc_slots": ["problem_statement"]},
                       self.reg, consensus=True, supersedes=v1)
        artlib.publish(self.app, "cB", {"type": "idea", "title": "S",
                                        "doc_slots": ["problem_statement"]},
                       self.reg, consensus=True, supersedes=v1)
        r = self.cov(docs._default_doc_map())["problem_statement"]
        self.assertEqual((r["status"], r["reason"]), ("empty", "empty"))
        self.assertIn("branch-heads:", r["evidence"])


class TestIdempotencyAndTransition(GapBase):
    def test_rerender_unchanged_publishes_no_new_gaps(self):
        # AC3: deterministic dedupe → re-render adds nothing.
        self.pub("TU", "target_user", "short")
        self.write()
        n = len(self.gaps())
        self.write()
        self.write()
        self.assertEqual(len(self.gaps()), n, "idempotent across re-renders")

    def test_fill_drops_slot_from_report_without_a_new_gap(self):
        # AC3 / D1 convention (b): a filled slot leaves the report; the stale
        # gap artifact persists (no close verb) but no NEW gap is published.
        self.write()
        n0 = len(self.gaps())
        self.assertIn("problem_statement",
                      {g["fields"]["slot_id"] for g in self.gaps()})
        self.pub("PS", "problem_statement", "x" * 300)   # fill it
        self.write()
        self.assertNotIn("problem_statement", self.report(),
                         "report reflects the current scan")
        self.assertEqual(len(self.gaps()), n0,
                         "no new gap; the stale empty gap persists")

    def test_thin_then_recontent_thin_emits_a_fresh_gap(self):
        # AC3: a slot that changes content (still thin, different bytes) → a new
        # key → a fresh gap (the content_hash is in the key).
        a = self.pub("T", "target_user", "aa")
        self.write()
        thin0 = [g for g in self.gaps()
                 if g["fields"].get("slot_id") == "target_user"]
        self.assertEqual(len(thin0), 1)
        artlib.publish(self.app, "bbbb", {"type": "idea", "title": "T",
                                          "doc_slots": ["target_user"]},
                       self.reg, consensus=True, supersedes=a)
        self.write()
        thin1 = [g for g in self.gaps()
                 if g["fields"].get("slot_id") == "target_user"]
        self.assertEqual(len(thin1), 2, "changed content → fresh thin gap")


class TestHappyPathAndFailure(GapBase):
    def test_fully_covered_project_reports_no_gaps(self):
        # AC1: all slots filled ≥ min_chars → zero gaps, honest report.
        for s in docs._default_doc_map()["slots"]:
            self.pub(s["slot_id"], s["slot_id"], "x" * (MC + 50))
        self.write()
        self.assertEqual(len(self.gaps()), 0)
        rep = self.report()
        self.assertIn("Handoff is complete", rep)
        self.assertIn("Filled: 40 / 40", rep)

    def test_gap_publish_failure_still_writes_report(self):
        # AC6: the report is never held hostage by the bus.
        self.pub("TU", "target_user", "short")

        class FailPub:
            def __getattr__(self, n):
                return getattr(artlib, n)

            def publish(self, *a, **k):
                return None

        written = self.write(reader=FailPub())
        self.assertIn("docs/GAP_REPORT.md", written)
        self.assertTrue(any("gap publish FAILED" in w for w in self.warns))
        self.assertEqual(len(self.gaps()), 0, "nothing published on failure")


class TestSchemaAndRouting(GapBase):
    def test_gap_meta_schema_round_trips(self):
        # AC7: the 7.5 stability-contract fields round-trip; enum + required set.
        self.pub("TU", "target_user", "short")
        self.write()
        thin = [g for g in self.gaps()
                if g["fields"].get("reason") == "thin"]
        self.assertEqual(len(thin), 1)
        f = thin[0]["fields"]
        for k in ("slot_id", "owner_section", "reason", "doc", "min_chars",
                  "observed_chars", "evidence", "dedupe_key"):
            self.assertIn(k, f)
        self.assertIn(f["reason"], ("empty", "thin", "lineage_conflict"))
        self.assertEqual(f["min_chars"], MC)
        self.assertEqual(self.reg["types"]["gap"]["required"],
                         ["title", "body", "impact"])

    def test_gaps_are_not_auto_routed(self):
        # AC4: the gap names its target section but nothing auto-injects.
        self.write()
        self.assertTrue(set(os.listdir(self.app)) <= {"docs", "artifacts"},
                        "no inbox/session dirs are created")
        g = self.gaps()[0]
        self.assertTrue(g["fields"]["owner_section"], "route target named")


if __name__ == "__main__":
    unittest.main()
