import XCTest
@testable import OrchestratorGUI

final class ConductorOversightTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent(".conductor/approvals"),
            withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func writeJSON(_ obj: Any, to url: URL) throws {
        let data = try JSONSerialization.data(
            withJSONObject: obj, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: url, options: .atomic)
    }

    func testScanReadsPersistedDialAndPendingRoute() throws {
        try writeJSON([
            "stage": "idle", "ledger_cursor": 4,
            "oversight": ["dial": "gated"]
        ], to: root.appendingPathComponent(
            ".conductor/conductor_state.json"))
        let routeID = "0123456789abcdef"
        try writeJSON([
            "route_id": routeID, "action_id": routeID,
            "target": "legal", "requested_by": "demo/ideas/chat-1",
            "reason": "gated oversight requires approval",
            "payload": ["artifact_id": "a-1", "rule_id": "idea:legal"]
        ], to: root.appendingPathComponent(
            ".conductor/approvals/\(routeID).pending"))

        let snapshot = ConductorOversightDisk.scan(rootURL: root)
        XCTAssertTrue(snapshot.available)
        XCTAssertEqual(snapshot.dial, .gated)
        XCTAssertEqual(snapshot.pending, [ConductorPendingRoute(
            routeID: routeID, artifactID: "a-1", target: "legal",
            ruleID: "idea:legal", requestedBy: "demo/ideas/chat-1",
            reason: "gated oversight requires approval")])
        XCTAssertTrue(snapshot.warnings.isEmpty)
    }

    func testCorruptStateShowsFallbackInsteadOfOptimisticDial() throws {
        try "{bad".write(to: root.appendingPathComponent(
            ".conductor/conductor_state.json"), atomically: true,
                         encoding: .utf8)
        let snapshot = ConductorOversightDisk.scan(rootURL: root)
        XCTAssertEqual(snapshot.dial, .loopsGated)
        XCTAssertEqual(snapshot.warnings.count, 1)
        XCTAssertTrue(snapshot.warnings[0].contains("unreadable"))
    }

    func testDialWriteUsesAtomicRequestAndNeverRewindsConductorState() throws {
        let url = root.appendingPathComponent(
            ".conductor/conductor_state.json")
        try writeJSON([
            "stage": "acting", "ledger_cursor": 19,
            "sessions": ["demo/ideas/chat-1": "digest"],
            "oversight": ["dial": "loops_gated"]
        ], to: url)
        XCTAssertNil(ConductorControlFiles.writeDial(
            rootURL: root, dial: .suggestOnly))
        let data = try Data(contentsOf: url)
        let obj = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(obj["ledger_cursor"] as? Int, 19)
        XCTAssertEqual((obj["sessions"] as? [String: String])?["demo/ideas/chat-1"],
                       "digest")
        XCTAssertEqual((obj["oversight"] as? [String: String])?["dial"],
                       "loops_gated", "UI waits for Conductor read-back")
        let requestData = try Data(contentsOf: root.appendingPathComponent(
            ".conductor/oversight_request.json"))
        let request = try XCTUnwrap(
            JSONSerialization.jsonObject(with: requestData) as? [String: String])
        XCTAssertEqual(request["dial"], "suggest_only")
    }

    func testDecisionWriterPinsSafeRouteAndKnownVerbs() throws {
        let routeID = "fedcba9876543210"
        XCTAssertNil(ConductorControlFiles.writeDecision(
            rootURL: root, routeID: routeID, suffix: "changes",
            body: "Needs a human check"))
        let body = try String(contentsOf: root.appendingPathComponent(
            ".conductor/approvals/\(routeID).changes"), encoding: .utf8)
        XCTAssertEqual(body, "Needs a human check\n")
        XCTAssertNotNil(ConductorControlFiles.writeDecision(
            rootURL: root, routeID: "../../escape", suffix: "ok"))
        XCTAssertNotNil(ConductorControlFiles.writeDecision(
            rootURL: root, routeID: routeID, suffix: "execute_anything"))
    }

    func testSubmittedDecisionDoesNotOptimisticallyRemovePendingCard() throws {
        let routeID = "fedcba9876543210"
        try writeJSON([
            "route_id": routeID, "target": "research",
            "requested_by": "demo/ideas/chat-1",
            "payload": ["artifact_id": "idea-1", "rule_id": "r1"]
        ], to: root.appendingPathComponent(
            ".conductor/approvals/\(routeID).pending"))
        XCTAssertNil(ConductorControlFiles.writeDecision(
            rootURL: root, routeID: routeID, suffix: "ok"))
        XCTAssertNil(ConductorControlFiles.writeDecision(
            rootURL: root, routeID: routeID, suffix: "ok"))

        let snapshot = ConductorOversightDisk.scan(rootURL: root)
        XCTAssertEqual(snapshot.pending.count, 1,
                       "card remains until the Conductor confirms in its ledger")
        XCTAssertTrue(snapshot.pending[0].decisionSubmitted)
        let files = try FileManager.default.contentsOfDirectory(atPath:
            root.appendingPathComponent(".conductor/approvals").path)
        XCTAssertEqual(files.filter { $0 == "\(routeID).ok" }.count, 1,
                       "double tap is one idempotent decision file")
    }
}
