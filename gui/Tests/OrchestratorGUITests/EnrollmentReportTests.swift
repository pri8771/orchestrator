import XCTest
@testable import OrchestratorGUI

final class EnrollmentReportTests: XCTestCase {
    private func tempProject() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("enrollment-report-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testEveryVerdictMapsToAnExplicitBadgeAndUnknownIsNeverOptimistic() {
        XCTAssertEqual(EnrollmentTrustBadge.from(verdict: "compliant"), .verified)
        XCTAssertEqual(EnrollmentTrustBadge.from(verdict: "non-compliant"), .flagged)
        XCTAssertEqual(EnrollmentTrustBadge.from(verdict: "not-applicable"), .notApplicable)
        XCTAssertEqual(EnrollmentTrustBadge.from(verdict: "cannot-determine"), .unverified)
        XCTAssertEqual(EnrollmentTrustBadge.from(verdict: "future-verdict"), .unverified)
        XCTAssertEqual(EnrollmentTrustBadge.from(verdict: ""), .unverified)
    }

    func testReportLoadsFinalArtifactLintGapsAndDistinctTrustAnnotations() throws {
        let project = try tempProject()
        let artifact = project.appendingPathComponent("artifacts/compliance-1")
        try FileManager.default.createDirectory(at: artifact,
                                                withIntermediateDirectories: true)
        let findings: [[String: Any]] = [
            ["rule": "knowledge/ios/privacy.md", "verdict": "compliant",
             "evidence_paths": ["Sources/Privacy.swift"], "why": "Observed manifest."],
            ["rule": "knowledge/ios/testing.md", "verdict": "cannot-determine",
             "evidence_paths": ["Package.swift"], "why": "No executed results."]
        ]
        let meta: [String: Any] = [
            "id": "compliance-1", "type": "compliance_report", "status": "final",
            "ts": "2026-07-21T12:00:00Z", "fields": ["findings": findings]
        ]
        try JSONSerialization.data(withJSONObject: meta)
            .write(to: artifact.appendingPathComponent("meta.json"))
        try Data("[UNVERIFIED] Runtime behavior was not observed.\n[RESEARCH: Apple HIG] External guidance.".utf8)
            .write(to: artifact.appendingPathComponent("body.md"))

        let docs = project.appendingPathComponent("docs")
        try FileManager.default.createDirectory(at: docs, withIntermediateDirectories: true)
        let lint: [String: Any] = ["violations": [[
            "kind": "fabricated_citation", "source": "doc_rebuild/doc.md",
            "line": 8, "detail": "cited path does not exist"
        ]]]
        try JSONSerialization.data(withJSONObject: lint)
            .write(to: docs.appendingPathComponent("provenance_lint.json"))

        let report = EnrollmentReportLoader.load(projectDir: project)
        XCTAssertEqual(report.findings.map(\.badge), [.verified, .unverified])
        XCTAssertEqual(report.findings[0].evidencePaths, ["Sources/Privacy.swift"])
        XCTAssertEqual(report.lintGaps.count, 1)
        XCTAssertEqual(report.lintGaps[0].location, "doc_rebuild/doc.md:8")
        XCTAssertEqual(report.provenanceNotes.map(\.kind), [.unverified, .research])
        XCTAssertTrue(report.warnings.isEmpty)
    }

    func testDraftArtifactAndUnreadableLintDoNotMasqueradeAsCleanEvidence() throws {
        let project = try tempProject()
        let artifact = project.appendingPathComponent("artifacts/draft")
        try FileManager.default.createDirectory(at: artifact,
                                                withIntermediateDirectories: true)
        try Data(#"{"type":"compliance_report","status":"draft","fields":{"findings":[]}}"#.utf8)
            .write(to: artifact.appendingPathComponent("meta.json"))
        let docs = project.appendingPathComponent("docs")
        try FileManager.default.createDirectory(at: docs, withIntermediateDirectories: true)
        try Data("{".utf8).write(to: docs.appendingPathComponent("provenance_lint.json"))

        let report = EnrollmentReportLoader.load(projectDir: project)
        XCTAssertTrue(report.findings.isEmpty)
        XCTAssertTrue(report.warnings.contains { $0.contains("No final compliance") })
        XCTAssertTrue(report.warnings.contains { $0.contains("unreadable") })
    }

    func testMalformedFinalFindingIsVisibleAsUnknownRatherThanSilentClean() throws {
        let project = try tempProject()
        let artifact = project.appendingPathComponent("artifacts/final")
        try FileManager.default.createDirectory(at: artifact,
                                                withIntermediateDirectories: true)
        let meta: [String: Any] = [
            "type": "compliance_report", "status": "final",
            "fields": ["findings": ["not-an-object"]]
        ]
        try JSONSerialization.data(withJSONObject: meta)
            .write(to: artifact.appendingPathComponent("meta.json"))
        let report = EnrollmentReportLoader.load(projectDir: project)
        XCTAssertTrue(report.findings.isEmpty)
        XCTAssertTrue(report.warnings.contains { $0.contains("malformed") })
    }

    func testEnrollmentCLIUsesE1ContractAndParsesOnlyPositiveCreatedEvidence() {
        XCTAssertEqual(EnrollmentCLI.arguments(
            engine: URL(fileURLWithPath: "/engine/orchestrator.py"),
            root: URL(fileURLWithPath: "/workspace"),
            source: URL(fileURLWithPath: "/their/repo")),
            ["/engine/orchestrator.py", "--root", "/workspace",
             "--enroll", "/their/repo"])
        XCTAssertEqual(EnrollmentCLI.createdSlug(
            output: "warning: not git\nENROLLED: adopted-repo\n"), "adopted-repo")
        XCTAssertNil(EnrollmentCLI.createdSlug(output: "Enrollment refused\n"))
    }

    func testBackgroundProjectMarksEnrollAndPromotedEnrollForSidebarBadge() throws {
        let root = try tempProject()
        let workflow = WorkflowDef(name: "enroll", title: "Enroll",
                                   description: "", target: "enroll", phases: [])
        func loaded(_ name: String, workflowName: String,
                    state: [String: Any] = [:]) throws -> Project {
            let dir = root.appendingPathComponent(name)
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try Data((workflowName + "\n").utf8)
                .write(to: dir.appendingPathComponent("workflow.txt"))
            if !state.isEmpty {
                try JSONSerialization.data(withJSONObject: state)
                    .write(to: dir.appendingPathComponent("agent_state.json"))
            }
            return try XCTUnwrap(BackgroundProjectLoader.loadProjects(
                names: [name], rootURL: root,
                workflowsByName: ["enroll": workflow, "iterate": workflow],
                defaultWorkflow: workflow, manualStops: [:],
                runningProcessNames: []).first)
        }

        XCTAssertTrue(try loaded("fresh-enroll", workflowName: "enroll").enrolled)
        XCTAssertTrue(try loaded(
            "promoted-enroll", workflowName: "iterate",
            state: ["promoted_from_enroll": ["target_path": "/their/repo"]]).enrolled)
        XCTAssertFalse(try loaded("ordinary", workflowName: "iterate").enrolled)
    }
}
