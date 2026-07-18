"""V3 board 4.7: PULL retrieval — build_context's artifact-retrieval layer.

artifacts.retrieve scores the project's ADMISSIBLE artifacts against a
phase query and returns a budgeted, provenance-headed block. Only the live
tip of each lineage participates; drafts, stale versions, converged
tombstones, branched lineages, and the session's own publications never
appear. Provenance headers survive the char budget; a corrupt meta is
skipped, never fatal.
"""
import json
import os
import shutil
import tempfile
import unittest

import artifacts as artlib


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="retrieval-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.orch_dir = os.path.join(self.tmp, "orch")
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)
        self.errors = []
        self.registry = artlib.load_registry(self.orch_dir,
                                              on_error=self.errors.append)
        artlib._ART_BODY_CACHE.clear()

    def _publish(self, title, body, type="idea", section="ideas",
                 session="chat-1", keywords=None, supersedes=None, **extra):
        meta = {"type": type, "title": title,
                "source": {"section": section, "session": session,
                           "phase": "brainstorm"}}
        if keywords is not None:
            meta["keywords"] = keywords
        meta.update(extra)
        # consensus=True so an auto type lands 'final' and is retrievable (4.8).
        return artlib.publish(self.project, body, meta, self.registry,
                              on_error=self.errors.append,
                              supersedes=supersedes, consensus=True)

    def _retrieve(self, query, **kw):
        kw.setdefault("on_error", self.errors.append)
        return artlib.retrieve(self.project, query, **kw)


class TestRetrieval(_Base):
    def test_relevant_artifact_injected_with_provenance(self):
        aid = self._publish("Dark mode toggle",
                            "Users want a dark mode theme everywhere.",
                            section="ideas", session="chat-a")
        out = self._retrieve("dark mode theme")
        self.assertIn("===== RELEVANT PROJECT ARTIFACTS =====", out)
        self.assertIn("----- idea %s v1 (from ideas/chat-a · phase "
                      "brainstorm) -----" % aid, out)
        self.assertIn("dark mode theme", out)

    def test_empty_and_irrelevant_inject_nothing(self):
        self.assertEqual(self._retrieve("anything"), "",
                         "empty store injects nothing")
        self._publish("Networking", "socket timeouts and retries",
                     keywords=["networking"])
        self.assertEqual(self._retrieve("dessert recipes"), "",
                         "an irrelevant store injects nothing")

    def test_top_k_and_budget_enforced(self):
        for i in range(5):
            self._publish("Cache idea %d" % i,
                          "caching strategy number %d " % i + "x " * 200,
                          session="s%d" % i)
        out = self._retrieve("caching strategy", top_k=2, max_chars=100000)
        self.assertEqual(out.count("----- idea"), 2, "top_k respected")
        tight = self._retrieve("caching strategy", top_k=5, max_chars=400)
        self.assertLessEqual(len(tight), 400, "char budget respected")

    def test_header_survives_truncation(self):
        aid = self._publish("Cache", "caching " + "z" * 20000,
                            keywords=["caching"])
        # Budget past the header but far under header+body.
        out = self._retrieve("caching", max_chars=300)
        self.assertLessEqual(len(out), 300)
        self.assertIn("----- idea %s v1 (from" % aid, out,
                      "the full provenance header survives")
        self.assertIn("truncated to fit", out)

    def test_header_never_partially_emitted(self):
        self._publish("Cache", "caching " + "z" * 5000, keywords=["caching"])
        # Budget smaller than the block header itself -> nothing injected
        # (never a half-header).
        self.assertEqual(self._retrieve("caching", max_chars=20), "")

    def test_scoring_keyword_beats_body_overlap(self):
        kw = self._publish("Alpha", "unrelated filler text here",
                           keywords=["caching"], session="s-kw")
        body = self._publish("Beta", "caching caching appears in the body",
                            keywords=["unrelated"], session="s-body")
        out = self._retrieve("caching", top_k=1)
        self.assertIn(kw, out, "one keyword hit (×3) outranks 2 body hits")
        self.assertNotIn(body, out)

    def test_drafts_and_stale_and_converged_excluded(self):
        # A draft.
        self._publish("Draft idea", "caching draft", status="draft",
                     keywords=["caching"])
        # A superseded v1 with a live v2.
        v1 = self._publish("Evolving", "caching v1 body", keywords=["caching"])
        v2 = artlib.publish(self.project, "caching v2 body",
                            {"type": "idea", "title": "Evolving",
                             "keywords": ["caching"],
                             "source": {"section": "ideas",
                                        "session": "chat-1",
                                        "phase": "brainstorm"}},
                            self.registry, supersedes=v1, consensus=True)
        out = self._retrieve("caching")
        self.assertNotIn("caching draft", out, "a draft is excluded")
        self.assertNotIn("caching v1 body", out,
                         "the stale predecessor is excluded")
        self.assertIn("idea %s v2 (from" % v2, out,
                      "only the live tip participates")
        self.assertNotIn("idea %s v1 (from" % v1, out)

    def test_self_published_excluded(self):
        self._publish("Mine", "caching by me", section="ideas",
                     session="chat-me", keywords=["caching"])
        self.assertEqual(
            self._retrieve("caching", exclude_session="chat-me",
                           exclude_section="ideas"),
            "", "the session's own publication is self-echo")
        self.assertIn("caching by me",
                      self._retrieve("caching", exclude_session="other",
                                     exclude_section="ideas"))

    def test_sibling_section_same_chat_slug_is_not_self_echo(self):
        # A chat slug is unique only within a section. The running
        # ideas/main session must still pull specs/main's published tip.
        self._publish("Spec brief", "caching in the spec",
                     section="specs", session="main", keywords=["caching"])
        out = self._retrieve("caching", exclude_session="main",
                             exclude_section="ideas")
        self.assertIn("caching in the spec", out,
                      "a sibling section's same-named chat is NOT self-echo")
        # Same section + same chat IS self-echo.
        self.assertEqual(
            self._retrieve("caching", exclude_session="main",
                           exclude_section="specs"), "")

    def test_hostile_meta_types_do_not_crash_retrieval(self):
        aid = self._publish("Good", "caching good", keywords=["caching"])
        # Hand-corrupt an admissible artifact's keywords (int) and title
        # (list) — retrieval must degrade, never raise a phase-fatal error.
        path = os.path.join(artlib.artifact_dir(self.project, aid),
                            "meta.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["keywords"] = 5
        doc["title"] = ["not", "a", "string"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        artlib._ART_BODY_CACHE.clear()
        # Must not raise; the body-term "caching" still scores.
        out = artlib.retrieve(self.project, "caching")
        self.assertIn("caching good", out)

    def test_branched_lineage_skipped_and_reported_once(self):
        r = self._publish("Base", "caching base", keywords=["caching"])
        a = artlib.publish(self.project, "caching path a",
                          {"type": "idea", "title": "A",
                           "keywords": ["caching"],
                           "source": {"section": "ideas", "session": "c",
                                      "phase": "p"}},
                          self.registry, supersedes=r)
        b = artlib.publish(self.project, "caching path b",
                          {"type": "idea", "title": "B",
                           "keywords": ["caching"],
                           "source": {"section": "ideas", "session": "c",
                                      "phase": "p"}},
                          self.registry, supersedes=r)
        errs = []
        out = artlib.retrieve(self.project, "caching", on_error=errs.append)
        self.assertEqual(out, "", "a branched lineage contributes nothing")
        branch_notes = [e for e in errs if "branched" in e]
        self.assertEqual(len(branch_notes), 1,
                         "the skip is reported exactly once, never guessed")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

    def test_corrupt_meta_skipped_with_on_error(self):
        good = self._publish("Good", "caching good", keywords=["caching"])
        bad = self._publish("Bad", "caching bad", keywords=["caching"])
        with open(os.path.join(artlib.artifact_dir(self.project, bad),
                               "meta.json"), "w") as fh:
            fh.write("{corrupt")
        errs = []
        out = artlib.retrieve(self.project, "caching", on_error=errs.append)
        self.assertIn(good, out, "the healthy artifact still retrieves")
        bad_notes = [e for e in errs if bad in e]
        self.assertEqual(len(bad_notes), 1,
                         "the corrupt meta is reported EXACTLY once "
                         "(one scan, not two)")

    def test_empty_lineage_with_supersedes_is_not_double_injected(self):
        # A hand-corrupted member (empty lineage but intact supersedes)
        # creates a second self-named root resolving to the same tip; the
        # tip-dedup must inject it once, never twice.
        r = self._publish("Base", "caching base", keywords=["caching"])
        child = artlib.publish(
            self.project, "caching child",
            {"type": "idea", "title": "Child", "keywords": ["caching"],
             "source": {"section": "ideas", "session": "c", "phase": "p"}},
            self.registry, supersedes=r, consensus=True)
        path = os.path.join(artlib.artifact_dir(self.project, child),
                            "meta.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["lineage"] = []                 # corrupt: empty, supersedes intact
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        artlib._ART_BODY_CACHE.clear()
        out = artlib.retrieve(self.project, "caching")
        self.assertEqual(out.count("----- idea %s v" % child), 1,
                         "the resolved tip is injected exactly once")

    def test_sensitivity_filter_threaded(self):
        self._publish("Idea one", "caching idea", type="idea",
                     keywords=["caching"], session="s1")
        self._publish("A gap", "caching gap", type="gap",
                     keywords=["caching"], session="s2", fields={"impact": "x"})
        out = artlib.retrieve(
            self.project, "caching",
            sensitivity_filter=lambda m: m.get("type") != "gap")
        self.assertIn("caching idea", out)
        self.assertNotIn("caching gap", out,
                         "the 8.5 seam filters by predicate")

    def test_body_is_read_once_via_cache(self):
        self._publish("Cache", "caching body text", keywords=["caching"])
        real = artlib.read_body
        calls = []
        def counting(pd, aid, on_error=None):
            calls.append(aid)
            return real(pd, aid, on_error=on_error)
        artlib.read_body = counting
        try:
            self._retrieve("caching")
            self._retrieve("caching")
        finally:
            artlib.read_body = real
        # One read for the (cached) terms across both retrievals.
        self.assertEqual(len(calls), 1,
                         "an unchanged body is read once, then stat-cached")


if __name__ == "__main__":
    unittest.main()
