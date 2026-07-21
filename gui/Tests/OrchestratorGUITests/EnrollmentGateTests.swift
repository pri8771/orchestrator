import XCTest
@testable import OrchestratorGUI

final class EnrollmentGateTests: XCTestCase {
    func testEnrollmentStatusDecodesAndUnknownStatusDegradesGracefully() {
        XCTAssertEqual(ProjectStatus.decode(
            engineValue: "enrolled_awaiting_approval", error: nil, done: false),
            .enrolledAwaitingApproval)
        XCTAssertEqual(ProjectStatus.decode(
            engineValue: "future_status", error: nil, done: false), .inProgress)
        XCTAssertEqual(ProjectStatus.decode(
            engineValue: "future_status", error: nil, done: true), .done)
    }

    func testPromotionEvidenceRequiresFinalComplianceArtifact() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let artifacts = root.appendingPathComponent("artifacts", isDirectory: true)
        let unrelated = artifacts.appendingPathComponent("artifact-1", isDirectory: true)
        try FileManager.default.createDirectory(at: unrelated,
                                                withIntermediateDirectories: true)
        try Data(#"{"type":"compliance_report","status":"draft"}"#.utf8)
            .write(to: unrelated.appendingPathComponent("meta.json"))
        XCTAssertFalse(EnrollmentEvidence.hasFinalComplianceReport(projectDir: root))

        let final = artifacts.appendingPathComponent("artifact-2", isDirectory: true)
        try FileManager.default.createDirectory(at: final,
                                                withIntermediateDirectories: true)
        try Data(#"{"type":"compliance_report","status":"final"}"#.utf8)
            .write(to: final.appendingPathComponent("meta.json"))
        XCTAssertTrue(EnrollmentEvidence.hasFinalComplianceReport(projectDir: root))
    }
}
