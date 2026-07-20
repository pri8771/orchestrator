import XCTest
@testable import OrchestratorGUI

final class MissionControlTests: XCTestCase {
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

    private func writeJSON(_ value: Any, _ path: String) throws {
        let url = root.appendingPathComponent(path)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(
            withJSONObject: value, options: [.sortedKeys])
        try data.write(to: url, options: .atomic)
    }

    private func appendLedger(_ records: [[String: Any]], trailing: String = "") throws {
        let text = try records.map { record -> String in
            let data = try JSONSerialization.data(
                withJSONObject: record, options: [.sortedKeys])
            return String(decoding: data, as: UTF8.self)
        }.joined(separator: "\n") + (records.isEmpty ? "" : "\n") + trailing
        try text.write(to: root.appendingPathComponent(
            ".conductor/conductor_ledger.jsonl"), atomically: true,
                       encoding: .utf8)
    }

    private func baseState(extra: [String: Any] = [:]) throws {
        var state: [String: Any] = [
            "stage": "idle", "oversight": ["dial": "loops_gated"]
        ]
        for (key, value) in extra { state[key] = value }
        try writeJSON(state, ".conductor/conductor_state.json")
    }

    func testNoConductorIsAnExplicitEmptySnapshot() throws {
        let absent = root.appendingPathComponent("absent")
        let snapshot = MissionControlDisk.scan(
            rootURL: absent, costs: [:], now: Date(timeIntervalSince1970: 5),
            isPidAlive: { _ in true })
        XCTAssertFalse(snapshot.available)
        XCTAssertFalse(snapshot.conductorRunning)
        XCTAssertTrue(snapshot.decisions.isEmpty)
        XCTAssertNil(snapshot.timeRange)
    }

    func testLedgerParsingSkipsCorruptAndCompletesPartialIncrementally() throws {
        try baseState()
        try "pid=4242 host=test\n".write(to: root.appendingPathComponent(
            ".conductor/conductor.lock"), atomically: true, encoding: .utf8)
        let complete: [String: Any] = [
            "ts": 10.0, "decision": "route_approved",
            "session": "demo/ideas/chat-1", "route_id": "0123456789abcdef",
            "detail": ["artifact_id": "idea-1", "target": "research",
                       "rule_id": "idea:research", "reason": "forward rule"]
        ]
        try appendLedger([complete], trailing: "not-json\n{\"ts\":20,")
        let first = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(timeIntervalSince1970: 30),
            isPidAlive: { $0 == 4242 })
        XCTAssertTrue(first.conductorRunning)
        XCTAssertEqual(first.decisions.map(\.decision), ["route_approved"])
        XCTAssertTrue(first.warnings.contains { $0.contains("unreadable") })
        XCTAssertEqual(first.frame().nodes.map(\.section), ["ideas", "research"])
        XCTAssertEqual(first.decisions[0].explanation,
                       "demo/ideas/chat-1 · artifact idea-1 · to research · rule idea:research · forward rule")
        let unchanged = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(timeIntervalSince1970: 30),
            isPidAlive: { _ in true })
        XCTAssertEqual(unchanged.warnings, first.warnings,
                       "durable corruption remains visibly disclosed on cached polls")

        let handle = try FileHandle(forWritingTo: root.appendingPathComponent(
            ".conductor/conductor_ledger.jsonl"))
        try handle.seekToEnd()
        try handle.write(contentsOf: Data(
            "\"decision\":\"stalled\",\"session\":\"demo/research/chat-2\",\"detail\":{\"reason\":\"vote undecided\"}}\n".utf8))
        try handle.close()
        let second = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(timeIntervalSince1970: 30),
            isPidAlive: { _ in false })
        XCTAssertFalse(second.conductorRunning)
        XCTAssertEqual(second.decisions.map(\.decision),
                       ["route_approved", "stalled"])
        XCTAssertEqual(second.decisions.last?.reason, "vote undecided")
    }

    func testUnchangedPollReadsNoLedgerBytesAndAppendReadsOnlyDelta() throws {
        try baseState()
        try appendLedger([
            ["ts": 10.0, "decision": "observed",
             "session": "demo/ideas/a", "detail": [:]],
        ])
        let ledgerURL = root.appendingPathComponent(
            ".conductor/conductor_ledger.jsonl")
        MissionControlDisk.resetLedgerCacheForTests(ledgerURL)
        _ = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(), isPidAlive: { _ in false })
        let initialBytes = MissionControlDisk.ledgerBytesReadForTests(ledgerURL)
        XCTAssertEqual(initialBytes, try Data(contentsOf: ledgerURL).count)

        _ = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(), isPidAlive: { _ in false })
        XCTAssertEqual(MissionControlDisk.ledgerBytesReadForTests(ledgerURL),
                       initialBytes, "unchanged background polls must not reparse the ledger")

        let appended = Data("{\"ts\":20,\"decision\":\"stalled\",\"session\":\"demo/ideas/a\",\"detail\":{}}\n".utf8)
        let handle = try FileHandle(forWritingTo: ledgerURL)
        try handle.seekToEnd()
        try handle.write(contentsOf: appended)
        try handle.close()
        let snapshot = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(), isPidAlive: { _ in false })
        XCTAssertEqual(snapshot.decisions.map(\.decision), ["observed", "stalled"])
        XCTAssertEqual(MissionControlDisk.ledgerBytesReadForTests(ledgerURL),
                       initialBytes + appended.count,
                       "append-only polls must read only bytes beyond the cached offset")
    }

    func testReplayFrameIsDeterministicReadOnlyAndUsesTimestampBoundary() throws {
        try baseState()
        try appendLedger([
            ["ts": 10.0, "decision": "observed",
             "session": "demo/ideas/a", "detail": [:]],
            ["ts": 20.0, "decision": "route_approved",
             "session": "demo/ideas/a", "route_id": "aaaaaaaaaaaaaaaa",
             "detail": ["artifact_id": "i1", "target": "research",
                        "rule_id": "r1"]],
            ["ts": 30.0, "decision": "stalled",
             "session": "demo/research/b", "detail": ["reason": "loop"]],
        ])
        let ledgerURL = root.appendingPathComponent(
            ".conductor/conductor_ledger.jsonl")
        let before = try Data(contentsOf: ledgerURL)
        let snapshot = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(timeIntervalSince1970: 40),
            isPidAlive: { _ in false })
        let atTwenty = snapshot.frame(at: Date(timeIntervalSince1970: 20))
        XCTAssertEqual(atTwenty.decisions.map(\.decision),
                       ["observed", "route_approved"])
        XCTAssertEqual(atTwenty.routes.map(\.target), ["research"])
        XCTAssertEqual(atTwenty,
                       snapshot.frame(at: Date(timeIntervalSince1970: 20)))
        XCTAssertEqual(before, try Data(contentsOf: ledgerURL),
                       "scrubbing is a pure projection and writes nothing")
    }

    func testLedgerConfirmationRemovesPendingCardNotDecisionSubmission() throws {
        try baseState()
        let routeID = "dddddddddddddddd"
        try writeJSON([
            "route_id": routeID, "target": "research",
            "requested_by": "demo/ideas/a",
            "payload": ["artifact_id": "i1", "rule_id": "idea:research"]
        ], ".conductor/approvals/\(routeID).pending")
        try "approved\n".write(to: root.appendingPathComponent(
            ".conductor/approvals/\(routeID).ok"), atomically: true,
            encoding: .utf8)
        try appendLedger([
            ["ts": 10.0, "decision": "approval_requested",
             "route_id": routeID,
             "detail": ["requested_by": "demo/ideas/a",
                        "artifact_id": "i1", "target": "research"]],
        ])
        let submitted = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(), isPidAlive: { _ in false })
        XCTAssertEqual(submitted.oversight.pending.count, 1)
        XCTAssertTrue(submitted.oversight.pending[0].decisionSubmitted)
        XCTAssertEqual(submitted.decisions[0].session, "demo/ideas/a",
                       "WHO falls back to the ledger's requested_by field")

        try appendLedger([
            ["ts": 10.0, "decision": "approval_requested",
             "route_id": routeID,
             "detail": ["requested_by": "demo/ideas/a",
                        "artifact_id": "i1", "target": "research"]],
            ["ts": 20.0, "decision": "route_approved",
             "route_id": routeID,
             "detail": ["requested_by": "demo/ideas/a",
                        "artifact_id": "i1", "target": "research"]],
        ])
        let confirmed = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(), isPidAlive: { _ in false })
        XCTAssertTrue(confirmed.oversight.pending.isEmpty,
                      "only a durable ledger confirmation clears the tray")
    }

    func testSnapshotTagsBecomeLabeledReplayDecisions() throws {
        try baseState()
        try FileManager.default.createDirectory(at: root.appendingPathComponent(
            ".git/refs/tags/conductor"), withIntermediateDirectories: true)
        try "object\n".write(to: root.appendingPathComponent(
            ".git/refs/tags/conductor/20260720T120000Z-7"),
                             atomically: true, encoding: .utf8)
        let snapshot = MissionControlDisk.scan(
            rootURL: root, costs: [:], now: Date(), isPidAlive: { _ in false })
        XCTAssertEqual(snapshot.snapshotTags.map(\.cursor), [7])
        XCTAssertEqual(snapshot.decisions.map(\.decision), ["snapshot"])
        XCTAssertEqual(snapshot.decisions[0].reason, "cursor 7")
    }

    func testBudgetMetersUsePersistedProviderSpendAndDistinctLimits() throws {
        try baseState(extra: [
            "halted": ["reason": "turns_exhausted"],
            "over_quota": ["google"]
        ])
        try writeJSON([
            "budgets": [
                "turns": 2, "wall_clock_s": 100,
                "per_provider": ["anthropic": ["spend": 3.0]]
            ]
        ], "goal_manifest.json")
        try appendLedger([
            ["ts": 10.0, "decision": "route_approved",
             "session": "demo/ideas/a", "route_id": "aaaaaaaaaaaaaaaa",
             "detail": ["artifact_id": "i1", "target": "research"]],
            ["ts": 20.0, "decision": "route_recovered",
             "session": "demo/research/b", "route_id": "bbbbbbbbbbbbbbbb",
             "detail": ["artifact_id": "r1", "target": "planning"]],
        ])
        var anthropic = CostTotals()
        anthropic.fold(metered: true, cost: 1_250_000)
        var project = ProjectCosts()
        project.total = anthropic
        project.byProvider["anthropic"] = anthropic
        let snapshot = MissionControlDisk.scan(
            rootURL: root, costs: ["demo/ideas/a": project],
            now: Date(timeIntervalSince1970: 70), isPidAlive: { _ in false })
        XCTAssertEqual(snapshot.budget.turnsUsed, 2)
        XCTAssertEqual(snapshot.budget.turnsCap, 2)
        XCTAssertEqual(snapshot.budget.wallClockSeconds, 60)
        XCTAssertEqual(snapshot.budget.wallClockCap, 100)
        XCTAssertEqual(snapshot.budget.byProvider["anthropic"]?.display, "$1.25")
        XCTAssertEqual(snapshot.budget.providerSpendCaps["anthropic"], 3.0)
        XCTAssertEqual(snapshot.budget.exhaustedReason, "turns_exhausted")
        XCTAssertEqual(snapshot.budget.overQuota, ["google"])
    }

    @MainActor
    func testRoutePreviewPrefersActualConductorTruthThenMarksDefault() throws {
        let store = OrchestratorStore()
        store.routePreviewSource = RoutePreviewSource { _ in "research" }
        let project = Project(
            name: "demo/ideas/chat-1", status: .done, currentPhase: nil,
            currentRound: 0, nextAgent: nil, error: nil, lastProcessed: nil,
            completedPhases: [], phaseOutputs: [:],
            dirURL: root.appendingPathComponent("demo/ideas/chat-1"))
        let fallback = try XCTUnwrap(store.routePreviewTarget(
            for: project,
            finalOutput: "```artifact-json\n{\"type\":\"idea\"}\n```"))
        XCTAssertEqual(fallback,
                       RoutePreviewPresentation(target: "research",
                                                truth: .routingDefault))

        store.missionControl.decisions = [MissionDecision(
            id: "actual", timestamp: Date(), decision: "approval_requested",
            session: project.name, artifactID: "idea-1", target: "legal",
            ruleID: "r2", reason: "capability", routeID: "cccccccccccccccc")]
        let actual = try XCTUnwrap(store.routePreviewTarget(
            for: project,
            finalOutput: "```artifact-json\n{\"type\":\"idea\"}\n```"))
        XCTAssertEqual(actual,
                       RoutePreviewPresentation(target: "legal",
                                                truth: .conductor))
    }
}
