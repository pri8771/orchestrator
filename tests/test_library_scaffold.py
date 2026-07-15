"""Tests for the library_mining package-build follow-on (NEXT_MILESTONES):
parse_extraction_blocks (orchestrator) + swiftscaffold + sanitize_swift_identifier
(schemas). The pure transform is fully unit-tested with hand-authored dicts; the
one functional "the generated skeleton actually compiles" test is gated on a
swift toolchain being present.
"""
import os
import shutil
import tempfile
import unittest

import orchestrator as orch
import schemas
import swiftscaffold
import verify


class TestSanitizeSwiftIdentifier(unittest.TestCase):
    def test_strips_spaces_and_punctuation(self):
        self.assertEqual(schemas.sanitize_swift_identifier("Network Kit!"), "NetworkKit")

    def test_leading_digit_prefixed(self):
        self.assertEqual(schemas.sanitize_swift_identifier("123abc"), "_123abc")

    def test_reserved_word_prefixed(self):
        self.assertEqual(schemas.sanitize_swift_identifier("class"), "_class")
        self.assertEqual(schemas.sanitize_swift_identifier("default"), "_default")
        self.assertEqual(schemas.sanitize_swift_identifier("func"), "_func")

    def test_unrecoverable_returns_none(self):
        self.assertIsNone(schemas.sanitize_swift_identifier(""))
        self.assertIsNone(schemas.sanitize_swift_identifier("!!!"))
        self.assertIsNone(schemas.sanitize_swift_identifier("   "))
        self.assertIsNone(schemas.sanitize_swift_identifier(None))
        self.assertIsNone(schemas.sanitize_swift_identifier(42))

    def test_valid_name_unchanged(self):
        self.assertEqual(schemas.sanitize_swift_identifier("APIClient"), "APIClient")


class TestParseExtractionBlocks(unittest.TestCase):
    def _block(self, body):
        return "prose above\n```extraction-json\n%s\n```\ntrailing prose" % body

    def test_valid_block_parses_and_normalizes(self):
        pkg, errs = orch.parse_extraction_blocks(self._block(
            '{"package_name": "NetworkKit", '
            '"public_api": [{"kind": "protocol", "name": "APIClient", '
            '"signature": "func send() async throws"}]}'))
        self.assertIsNotNone(pkg)
        self.assertEqual(pkg["package_name"], "NetworkKit")
        self.assertEqual(len(pkg["public_api"]), 1)
        self.assertEqual(pkg["public_api"][0]["name"], "APIClient")
        self.assertEqual(errs, [])

    def test_last_block_wins(self):
        text = (self._block('{"package_name": "Kit", "public_api": '
                            '[{"kind": "struct", "name": "First"}]}')
                + "\n"
                + self._block('{"package_name": "Kit", "public_api": '
                             '[{"kind": "struct", "name": "Second"}]}'))
        pkg, _ = orch.parse_extraction_blocks(text)
        self.assertEqual([i["name"] for i in pkg["public_api"]], ["Second"])

    def test_missing_required_reported_not_silent(self):
        # public_api is a required field.
        pkg, errs = orch.parse_extraction_blocks(self._block('{"package_name": "Kit"}'))
        self.assertIsNone(pkg)
        self.assertTrue(errs)

    def test_malformed_json_does_not_raise(self):
        pkg, errs = orch.parse_extraction_blocks(self._block("{not json}"))
        self.assertIsNone(pkg)
        self.assertTrue(errs)

    def test_invalid_kind_item_skipped_with_error(self):
        pkg, errs = orch.parse_extraction_blocks(self._block(
            '{"package_name": "Kit", "public_api": ['
            '{"kind": "widget", "name": "Bad"}, '
            '{"kind": "struct", "name": "Good"}]}'))
        self.assertEqual([i["name"] for i in pkg["public_api"]], ["Good"])
        self.assertTrue(any("widget" in e for e in errs))

    def test_identifiers_are_sanitized_in_output(self):
        pkg, _ = orch.parse_extraction_blocks(self._block(
            '{"package_name": "My Cool Kit!", "public_api": ['
            '{"kind": "func", "name": "do it now"}]}'))
        self.assertEqual(pkg["package_name"], "MyCoolKit")
        self.assertEqual(pkg["public_api"][0]["name"], "doitnow")

    def test_dedup_by_sanitized_name_last_wins(self):
        # "API-Client" and "APIClient" both sanitize to "APIClient".
        pkg, _ = orch.parse_extraction_blocks(self._block(
            '{"package_name": "Kit", "public_api": ['
            '{"kind": "protocol", "name": "API-Client"}, '
            '{"kind": "struct", "name": "APIClient"}]}'))
        self.assertEqual(len(pkg["public_api"]), 1)
        self.assertEqual(pkg["public_api"][0]["kind"], "struct")   # last wins

    def test_unusable_package_name_returns_none(self):
        pkg, errs = orch.parse_extraction_blocks(self._block(
            '{"package_name": "!!!", "public_api": [{"kind": "struct", "name": "X"}]}'))
        self.assertIsNone(pkg)
        self.assertTrue(errs)

    def test_no_block_returns_none_no_error(self):
        pkg, errs = orch.parse_extraction_blocks("just prose, no fence")
        self.assertIsNone(pkg)
        self.assertEqual(errs, [])


class TestScaffoldWritesTree(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest, ignore_errors=True)

    def _pkg(self):
        return {"package_name": "NetworkKit",
                "public_api": [{"kind": "protocol", "name": "APIClient",
                                "signature": "func send() async throws -> Data"}]}

    def test_writes_expected_files(self):
        m = swiftscaffold.scaffold_spm_package(self._pkg(), self.dest)
        self.assertTrue(m["ok"], m)
        self.assertEqual(m["api_count"], 1)
        pkgdir = m["package_dir"]
        self.assertTrue(os.path.exists(os.path.join(pkgdir, "Package.swift")))
        self.assertTrue(os.path.exists(
            os.path.join(pkgdir, "Sources", "NetworkKit", "NetworkKit.swift")))
        self.assertIn("Package.swift", m["files"])

    def test_idempotent_overwrite(self):
        m1 = swiftscaffold.scaffold_spm_package(self._pkg(), self.dest)
        m2 = swiftscaffold.scaffold_spm_package(self._pkg(), self.dest)
        self.assertEqual(m1["files"], m2["files"])
        self.assertEqual(m1["package_dir"], m2["package_dir"])

    def test_path_is_from_sanitized_name_no_traversal(self):
        # A hostile raw name must never escape dest_dir; scaffold re-sanitizes.
        m = swiftscaffold.scaffold_spm_package(
            {"package_name": "../../etc/evil", "public_api": []}, self.dest)
        self.assertTrue(m["ok"])
        # The package dir stays strictly under dest_dir.
        self.assertTrue(os.path.realpath(m["package_dir"]).startswith(
            os.path.realpath(self.dest)))

    def test_unusable_name_reports_error_not_raise(self):
        m = swiftscaffold.scaffold_spm_package({"package_name": "!!!"}, self.dest)
        self.assertFalse(m["ok"])
        self.assertTrue(m["errors"])

    def test_every_kind_renders_public_decl(self):
        pkg = {"package_name": "Kit", "public_api": [
            {"kind": "protocol", "name": "P", "signature": "proto sig"},
            {"kind": "struct", "name": "S", "signature": "struct sig"},
            {"kind": "class", "name": "C", "signature": "class sig"},
            {"kind": "enum", "name": "E", "signature": "enum sig"},
            {"kind": "func", "name": "f", "signature": "func f() -> Int"},
        ]}
        src = swiftscaffold.render_sources_swift(pkg)
        self.assertIn("public protocol P", src)
        self.assertIn("public struct S", src)
        self.assertIn("public final class C", src)
        self.assertIn("public enum E", src)
        self.assertIn("public func f()", src)
        # Signatures ride along ONLY as doc comments, never as live code.
        for sig in ("proto sig", "struct sig", "enum sig", "func f() -> Int"):
            self.assertIn("/// Proposed: %s" % sig, src)

    def test_multiline_signature_cannot_escape_doc_comment(self):
        # A multi-line / injection-y signature must collapse to a single ///
        # line — the injected text may appear, but ONLY inside the comment,
        # never as live Swift.
        pkg = {"package_name": "Kit", "public_api": [
            {"kind": "struct", "name": "S",
             "signature": "line one\n}\npublic struct Injected {}\n/// "}]}
        src = swiftscaffold.render_sources_swift(pkg)
        # Every line mentioning the injected decl must be a doc comment.
        for line in src.splitlines():
            if "public struct Injected" in line:
                self.assertTrue(line.lstrip().startswith("///"),
                                "injected code escaped the comment: %r" % line)
        # Only the legit stub declares a struct as live code — the injection
        # didn't add a second one.
        live_structs = [ln for ln in src.splitlines()
                        if ln.lstrip().startswith("public struct ")]
        self.assertEqual(live_structs, ["public struct S {"])
        # The whole signature is flattened onto ONE doc-comment line.
        self.assertIn("/// Proposed: line one } public struct Injected {} ///", src)

    def test_empty_public_api_still_valid_source(self):
        src = swiftscaffold.render_sources_swift({"package_name": "Kit", "public_api": []})
        self.assertIn("import Foundation", src)

    def test_package_manifest_is_templated_from_name_only(self):
        manifest = swiftscaffold.render_package_swift({"package_name": "NetworkKit"})
        self.assertIn('name: "NetworkKit"', manifest)
        self.assertIn('.library(name: "NetworkKit", targets: ["NetworkKit"])', manifest)
        self.assertIn('.target(name: "NetworkKit")', manifest)
        # No test target (swift build wouldn't compile it — no unverified surface).
        self.assertNotIn("testTarget", manifest)


class TestPhaseContractRequestsExtraction(unittest.TestCase):
    def _phasedef(self, key):
        # _phase_contract accepts either a namedtuple-ish with .key or a tuple.
        return (key, key, "%s.md" % key, "purpose")

    def test_library_mining_extraction_phase_requests_block(self):
        cfg = {"_workflow_target": "library_mining"}
        contract = orch._phase_contract(cfg, self._phasedef("extraction_candidates"))
        self.assertIn("extraction-json", contract)

    def test_other_workflow_same_phase_key_does_not_inherit(self):
        cfg = {"_workflow_target": "app"}
        contract = orch._phase_contract(cfg, self._phasedef("extraction_candidates"))
        self.assertNotIn("extraction-json", contract)


@unittest.skipUnless(shutil.which("swift"), "swift toolchain not installed")
class TestGeneratedSkeletonCompiles(unittest.TestCase):
    """The core guarantee: a scaffolded package actually builds. Covers all five
    API kinds plus the empty-API case."""

    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest, ignore_errors=True)

    def _build(self, pkg):
        m = swiftscaffold.scaffold_spm_package(pkg, self.dest)
        self.assertTrue(m["ok"], m)
        res = verify._verify_spm(m["package_dir"], 300)
        self.assertTrue(res["ran"], res)
        self.assertTrue(res["ok"], "generated package failed to compile:\n%s"
                        % res.get("errors"))

    def test_all_kinds_compile(self):
        self._build({"package_name": "AllKinds", "public_api": [
            {"kind": "protocol", "name": "P", "signature": "func p() -> Undeclared"},
            {"kind": "struct", "name": "S", "signature": "var x: AlsoUndeclared"},
            {"kind": "class", "name": "C", "signature": "init(z: Missing)"},
            {"kind": "enum", "name": "E", "signature": "case a(Nope)"},
            {"kind": "func", "name": "run", "signature": "func run() throws -> Ghost"},
        ]})

    def test_reserved_word_names_compile(self):
        # Names colliding with Swift keywords must still produce a buildable pkg.
        self._build({"package_name": "class", "public_api": [
            {"kind": "struct", "name": "default"},
            {"kind": "func", "name": "func"},
        ]})

    def test_empty_api_compiles(self):
        self._build({"package_name": "EmptyKit", "public_api": []})


if __name__ == "__main__":
    unittest.main()
