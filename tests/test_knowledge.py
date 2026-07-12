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


class TestShouldInject(unittest.TestCase):
    def test_returns_bool(self):
        self.assertIsInstance(k.should_inject("tech_specs"), bool)


if __name__ == "__main__":
    unittest.main()
