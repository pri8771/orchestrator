import json, os, tempfile, unittest
import docs
import orchestrator as orch

# Obviously fake, constructed so no real credential ever sits in the repo.
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


class TestScanBuildSecrets(unittest.TestCase):
    def setUp(self):
        self.build_dir = tempfile.mkdtemp()

    def _write(self, rel, content, mode="w"):
        path = os.path.join(self.build_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(content)

    def test_planted_fake_secret_found_with_location_not_value(self):
        self._write("App/Config.swift",
                    "import Foundation\nlet region = \"us-east-1\"\n"
                    "let awsKey = \"%s\"\n" % FAKE_AWS)
        findings = orch.scan_build_secrets(self.build_dir)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["source"], "secret_scan")
        self.assertEqual(f["category"], "secret_hardcoded")
        self.assertEqual(f["severity"], "Critical")
        self.assertEqual(f["status"], "open")
        self.assertIn("App/Config.swift:3", f["title"])
        self.assertIn("aws_key", f["title"])
        # NEVER the secret value itself, anywhere in the finding.
        self.assertNotIn(FAKE_AWS, json.dumps(findings))

    def test_clean_tree_yields_no_findings(self):
        self._write("App/Main.swift", "struct Main {}\nlet name = \"habit\"\n")
        self.assertEqual(orch.scan_build_secrets(self.build_dir), [])

    def test_git_dir_binaries_and_huge_files_skipped(self):
        self._write(".git/objects/leak.txt", FAKE_AWS + "\n")
        self._write("blob.bin", b"\x00\x01" + FAKE_AWS.encode(), mode="wb")
        self._write("huge.txt", (" " * (1024 * 1024)) + FAKE_AWS)
        self.assertEqual(orch.scan_build_secrets(self.build_dir), [])

    def test_placeholder_allowlisted(self):
        self._write("App/Sample.swift", 'let apiKey = "example_placeholder_key"\n')
        self.assertEqual(orch.scan_build_secrets(self.build_dir), [])

    def test_missing_dir_is_empty(self):
        self.assertEqual(orch.scan_build_secrets(os.path.join(self.build_dir, "nope")), [])


class TestFindingsMerge(unittest.TestCase):
    def test_merge_replaces_secret_scan_keeps_other_sources(self):
        app_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(app_dir, "docs"))
        stale = {"source": "secret_scan", "category": "secret_hardcoded",
                 "severity": "Critical", "title": "stale hit", "status": "open"}
        review = {"source": "code_review", "category": "bug",
                  "severity": "High", "title": "review hit", "status": "open"}
        with open(os.path.join(app_dir, "docs", "findings.json"), "w") as fh:
            json.dump({"schema_version": 1, "findings": [stale, review]}, fh)
        fresh = {"source": "secret_scan", "category": "secret_hardcoded",
                 "severity": "Critical", "title": "fresh hit", "status": "open"}
        merged = orch.merge_secret_findings(app_dir, [fresh])
        titles = [f["title"] for f in merged]
        self.assertIn("review hit", titles)
        self.assertIn("fresh hit", titles)
        self.assertNotIn("stale hit", titles)
        self.assertEqual(orch.load_docs_findings(app_dir), merged)

    def test_load_missing_findings_is_none(self):
        self.assertIsNone(orch.load_docs_findings(tempfile.mkdtemp()))


class TestSecretGateInLaunchReadiness(unittest.TestCase):
    PHASES = [("build_coordination", "Build")]
    OUTPUTS = {"build_coordination": "built"}

    def test_gate_fails_with_unresolved_secret_finding(self):
        finding = {"source": "secret_scan", "category": "secret_hardcoded",
                   "severity": "Critical", "title": "Hardcoded secret (aws_key) at a.swift:3",
                   "status": "open"}
        md = docs.render_launch_readiness("Demo", self.PHASES, self.OUTPUTS, {},
                                          findings=[finding])
        self.assertIn("Secret scan: FAIL — 1 unresolved secret_hardcoded finding(s)", md)
        self.assertIn("LAUNCH BLOCKED", md)

    def test_gate_passes_when_scan_ran_clean(self):
        md = docs.render_launch_readiness("Demo", self.PHASES, self.OUTPUTS, {},
                                          findings=[])
        self.assertIn("Secret scan: PASS (0 hardcoded secrets)", md)
        self.assertNotIn("LAUNCH BLOCKED", md)

    def test_gate_ignores_fixed_findings(self):
        finding = {"source": "secret_scan", "category": "secret_hardcoded",
                   "severity": "Critical", "title": "old", "status": "fixed"}
        md = docs.render_launch_readiness("Demo", self.PHASES, self.OUTPUTS, {},
                                          findings=[finding])
        self.assertIn("Secret scan: PASS (0 hardcoded secrets)", md)

    def test_gate_honest_when_never_scanned(self):
        md = docs.render_launch_readiness("Demo", self.PHASES, self.OUTPUTS, {},
                                          findings=None)
        self.assertIn("Secret scan: not run", md)


if __name__ == "__main__":
    unittest.main()
