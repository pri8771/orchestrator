"""Tests for knowledge.py — domain routing + keyword retrieval/RAG injection."""
import os
import tempfile
import unittest

import knowledge as k

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDomainFor(unittest.TestCase):
    def test_explicit_config_wins(self):
        self.assertEqual(k.domain_for("backend", "app", "some ios swiftui text"), "backend")

    def test_productionize_is_backend(self):
        self.assertEqual(k.domain_for("", "productionize", "anything"), "backend")

    def test_ios_markers(self):
        self.assertEqual(k.domain_for("", "app", "a SwiftUI app using SwiftData"), "ios")

    def test_web_markers(self):
        self.assertEqual(k.domain_for("", "app", "a Next.js React web app with Tailwind"), "web")

    def test_general_fallback_has_a_real_dir(self):
        # A non-iOS/web/backend project falls back to "general", which must be a
        # real domain (else it silently gets zero knowledge injection).
        self.assertEqual(k.domain_for("", "app", "a command line todo tool"), "general")
        self.assertIn("general", k.available_domains(HERE))


class TestRetrieve(unittest.TestCase):
    def test_returns_relevant_content_for_general(self):
        out = k.retrieve(HERE, "general",
                         "error handling and tests for a cli tool", max_chars=4000)
        self.assertTrue(out.strip())
        # header present and content included
        self.assertIn("KNOWLEDGE", out.upper())

    def test_respects_char_budget(self):
        out = k.retrieve(HERE, "ios", "swiftui architecture persistence testing",
                         max_chars=1500)
        self.assertLessEqual(len(out), 1500 + 500)  # budget + header slack

    def test_missing_domain_dir_is_empty_not_error(self):
        self.assertEqual(k.retrieve(HERE, "nonexistent-domain", "anything"), "")

    def test_empty_query_never_raises(self):
        k.retrieve(HERE, "general", "")
        k.retrieve(HERE, "general", None)

    def test_no_knowledge_dir_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(k.retrieve(d, "general", "x"), "")
            self.assertEqual(k.available_domains(d), [])

    def test_header_not_emitted_when_no_content(self):
        # An unmatched/empty domain must not prepend a lonely "RELEVANT
        # KNOWLEDGE" header with nothing under it.
        out = k.retrieve(HERE, "nonexistent-domain", "unrelated query terms")
        self.assertNotIn("KNOWLEDGE", out.upper())

    def test_docs_read_from_disk_once_across_repeated_queries(self):
        with tempfile.TemporaryDirectory() as d:
            domain_dir = os.path.join(d, "knowledge", "general")
            os.makedirs(domain_dir)
            doc_path = os.path.join(domain_dir, "one.md")
            with open(doc_path, "w", encoding="utf-8") as fh:
                fh.write("<!-- keywords: widgets -->\nAll about widgets.\n")
            real_open = open
            calls = []

            def counting_open(p, *a, **kw):
                if p == doc_path:
                    calls.append(p)
                return real_open(p, *a, **kw)

            import unittest.mock
            with unittest.mock.patch("builtins.open", counting_open):
                out1 = k.retrieve(d, "general", "widgets")
                out2 = k.retrieve(d, "general", "widgets and gizmos")
            self.assertEqual(len(calls), 1)
            self.assertIn("widgets", out1.lower())
            self.assertIn("widgets", out2.lower())

    def test_same_mtime_different_size_replacement_busts_cache(self):
        # A same-second file replacement used to serve a stale parse because
        # the cache key was mtime alone; keying on (mtime_ns, size) catches a
        # replacement whose length differs even with an identical mtime.
        with tempfile.TemporaryDirectory() as d:
            domain_dir = os.path.join(d, "knowledge", "general")
            os.makedirs(domain_dir)
            doc_path = os.path.join(domain_dir, "one.md")
            with open(doc_path, "w", encoding="utf-8") as fh:
                fh.write("<!-- keywords: widgets -->\nAll about widgets.\n")
            st = os.stat(doc_path)
            self.assertIn("all about widgets",
                          k.retrieve(d, "general", "widgets").lower())  # prime
            with open(doc_path, "w", encoding="utf-8") as fh:
                fh.write("<!-- keywords: widgets -->\nFresh widget content here.\n")
            # Pin the mtime back to the original — simulates a same-second
            # replacement that a coarse mtime key can't distinguish.
            os.utime(doc_path, ns=(st.st_atime_ns, st.st_mtime_ns))
            out = k.retrieve(d, "general", "widgets")
            self.assertIn("fresh widget content", out.lower())


class TestMultiWordKeywords(unittest.TestCase):
    """Multi-word `<!-- keywords: ... -->` phrases used to be stored verbatim
    and could never intersect the single-token query terms — dead weight in
    every real knowledge doc. They are now tokenized into words."""

    def test_multi_word_phrase_now_retrieves(self):
        with tempfile.TemporaryDirectory() as d:
            domain_dir = os.path.join(d, "knowledge", "general")
            os.makedirs(domain_dir)
            with open(os.path.join(domain_dir, "ds.md"), "w", encoding="utf-8") as fh:
                fh.write("<!-- keywords: design system -->\nToken palette guidance.\n")
            out = k.retrieve(d, "general", "build a design system for the app")
            self.assertIn("token palette guidance", out.lower())

    def test_phrase_words_score_as_strong_keywords(self):
        # Both words of the phrase count as keyword (x3) hits, so a doc whose
        # only match is body text loses to the keyword-annotated doc.
        kws = k._doc_keywords("<!-- keywords: refresh token rotation -->\nbody\n")
        self.assertIn("refresh", kws)
        self.assertIn("token", kws)
        self.assertIn("rotation", kws)
        self.assertNotIn("refresh token rotation", kws)

    def test_single_word_keywords_unchanged(self):
        # Single-word entries (incl. _WORD_RE-friendly dotted ones) still
        # yield exactly themselves.
        kws = k._doc_keywords("<!-- keywords: fastapi, next.js, @observable -->\n# T\n")
        self.assertLessEqual({"fastapi", "next.js", "@observable"}, kws)

    def test_real_knowledge_doc_multi_word_annotation_matches(self):
        # A real curated doc: knowledge/web/react-nextjs-architecture.md is
        # annotated with multi-word phrases like "partial prerendering" and
        # "intercepting routes". Those phrases were dead weight before the
        # tokenization fix; their words must now be strong keywords, and a
        # query built from them must retrieve the doc.
        doc = os.path.join(HERE, "knowledge", "web", "react-nextjs-architecture.md")
        kws, _terms, _body = k._load_doc(doc)
        self.assertLessEqual({"partial", "prerendering", "intercepting", "routes"}, kws)
        out = k.retrieve(HERE, "web",
                         "routing with route groups, intercepting routes and "
                         "partial prerendering in the app router")
        self.assertIn("react nextjs architecture", out.lower())


class TestShouldInject(unittest.TestCase):
    def test_returns_bool(self):
        self.assertIsInstance(k.should_inject("tech_specs"), bool)


if __name__ == "__main__":
    unittest.main()
