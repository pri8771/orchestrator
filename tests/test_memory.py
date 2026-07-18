"""V3 board 4.13: factory memory — founder-pinned facts retrieved and
injected at the phase-start slot. Scope (global/project/section) + trigger
(pinned vs keyword) filtering, budget precedence (section > project >
global), provenance headers, malformed-fact banners, and inheritance by a
spawned sub-session.
"""
import os
import shutil
import tempfile
import unittest

import memory as memlib


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memory-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.orch = os.path.join(self.tmp, "orch")
        self.md = memlib.memory_dir(self.orch)
        os.makedirs(self.md)
        self.errors = []
        memlib._FACT_CACHE.clear()

    def _fact(self, name, body, **fm):
        lines = ["---"]
        for k, v in fm.items():
            lines.append("%s: %s" % (k, v))
        lines.append("---")
        lines.append(body)
        with open(os.path.join(self.md, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _ret(self, project="proj", section="ideas", query="the phase query",
             max_chars=3000):
        return memlib.retrieve(self.orch, project, section, query,
                               max_chars=max_chars,
                               on_error=self.errors.append)


class TestScope(_Base):
    def test_scope_matrix(self):
        self._fact("g.md", "use SwiftUI only", scope="global")
        self._fact("p.md", "proj uses RevenueCat", scope="project",
                   project="proj")
        self._fact("s.md", "ideas section note", scope="section",
                   section="ideas")
        self._fact("other_p.md", "other project fact", scope="project",
                   project="other")
        self._fact("other_s.md", "other section fact", scope="section",
                   section="research")
        out = self._ret(project="proj", section="ideas")
        self.assertIn("use SwiftUI only", out)          # global always
        self.assertIn("proj uses RevenueCat", out)      # matching project
        self.assertIn("ideas section note", out)        # matching section
        self.assertNotIn("other project fact", out)
        self.assertNotIn("other section fact", out)

    def test_section_facts_absent_in_legacy_run(self):
        self._fact("g.md", "global fact", scope="global")
        self._fact("s.md", "section fact", scope="section", section="ideas")
        out = memlib.retrieve(self.orch, "proj", "", "q")  # no section
        self.assertIn("global fact", out)
        self.assertNotIn("section fact", out)

    def test_provenance_headers(self):
        self._fact("swift.md", "SwiftUI only", scope="global")
        out = self._ret()
        self.assertIn("===== FACTORY MEMORY (founder-pinned — binding) =====",
                      out)
        self.assertIn("----- swift.md (global) -----", out)


class TestTriggers(_Base):
    def test_pinned_vs_triggered(self):
        self._fact("pinned.md", "always applies", scope="global")
        self._fact("trig.md", "auth guidance", scope="global",
                   triggers="auth, refresh token")
        # Query without the trigger term: only the pinned fact.
        out = self._ret(query="build the home screen")
        self.assertIn("always applies", out)
        self.assertNotIn("auth guidance", out)
        # Query with a trigger term (multi-word trigger tokenizes).
        out2 = self._ret(query="handle the refresh token rotation")
        self.assertIn("auth guidance", out2)

    def test_comma_trigger_tokenization(self):
        self._fact("t.md", "iap note", scope="global",
                   triggers="in-app purchase, revenuecat")
        # "in-app" tokenizes to {in, app}; a query with "app" hits.
        self.assertIn("iap note", self._ret(query="the app store flow"))
        self.assertIn("iap note", self._ret(query="use revenuecat here"))
        self.assertNotIn("iap note", self._ret(query="unrelated query"))


class TestBudgetPrecedence(_Base):
    def test_precedence_under_budget(self):
        # Each fact ~40 chars; a budget that fits header + 2 facts drops the
        # least specific (global).
        self._fact("g.md", "G" * 40, scope="global")
        self._fact("p.md", "P" * 40, scope="project", project="proj")
        self._fact("s.md", "S" * 40, scope="section", section="ideas")
        hdr = len(memlib.MEMORY_HEADER)
        block = len("\n\n----- s.md (section) -----\n") + 40
        out = self._ret(max_chars=hdr + block * 2 + 5)
        self.assertIn("S" * 40, out, "section survives")
        self.assertIn("P" * 40, out, "project survives")
        self.assertNotIn("G" * 40, out, "global is dropped first")

    def test_empty_and_missing_inject_nothing(self):
        self.assertEqual(self._ret(), "", "empty memory dir injects nothing")
        shutil.rmtree(self.md)
        self.assertEqual(self._ret(), "", "absent memory dir injects nothing")


class TestMalformed(_Base):
    def test_malformed_frontmatter_skips_only_that_fact(self):
        self._fact("good.md", "good fact", scope="global")
        # Unclosed frontmatter.
        with open(os.path.join(self.md, "bad.md"), "w") as fh:
            fh.write("---\nscope: global\nno closing fence\ngood body\n")
        out = self._ret()
        self.assertIn("good fact", out)
        self.assertTrue(any("bad.md" in e for e in self.errors),
                        "the malformed fact is reported, not silent")

    def test_invalid_scope_and_missing_required_skip(self):
        self._fact("badscope.md", "x", scope="planetary")
        self._fact("noproj.md", "x", scope="project")     # missing project
        self.assertEqual(self._ret(), "")
        self.assertEqual(len(self.errors), 2, self.errors)

    def test_no_frontmatter_is_a_global_pinned_fact(self):
        with open(os.path.join(self.md, "plain.md"), "w") as fh:
            fh.write("just a body, no frontmatter\n")
        self.assertIn("just a body", self._ret())

    def test_fact_cached_by_stat(self):
        self._fact("c.md", "cached", scope="global")
        real = memlib.parse_fact
        calls = []
        def counting(path, on_error=None):
            calls.append(path)
            return real(path, on_error=on_error)
        memlib.parse_fact = counting
        try:
            self._ret()
            self._ret()
        finally:
            memlib.parse_fact = real
        self.assertEqual(len(calls), 1, "an unchanged fact is parsed once")


class TestStaging(unittest.TestCase):
    """Memory reaches a phase's build_context, and a 4.4-spawned session
    inherits it (staging is engine-side per phase)."""

    def setUp(self):
        import orchestrator as orch
        self.orch = orch
        # Plant a global fact in the ENGINE memory dir (HERE), cleaned up.
        self.mdir = memlib.memory_dir(orch.HERE)
        self._existed = os.path.isdir(self.mdir)
        os.makedirs(self.mdir, exist_ok=True)
        self.fact = os.path.join(self.mdir, "_test_zzz_swiftui.md")
        with open(self.fact, "w", encoding="utf-8") as fh:
            fh.write("---\nscope: global\n---\nUSE SWIFTUI ONLY marker\n")
        memlib._FACT_CACHE.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            os.remove(self.fact)
        except OSError:
            pass
        if not self._existed:
            try:
                os.rmdir(self.mdir)
            except OSError:
                pass

    def test_memory_reaches_build_context(self):
        import workflows as wf
        ctx = self.orch.build_context(
            {"root": "/tmp", "_memory": memlib.retrieve(
                self.orch.HERE, "p", "ideas", "any query")},
            "app", ("k", "f", "f.md", "purpose"), "prompt", [], "t")
        self.assertIn("USE SWIFTUI ONLY marker", ctx)
        self.assertIn("FACTORY MEMORY", ctx)


if __name__ == "__main__":
    unittest.main()
