import XCTest
@testable import OrchestratorGUI

final class ConductorNotificationTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suite: String!

    override func setUp() {
        super.setUp()
        suite = "ConductorNotificationTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        defaults = nil
        super.tearDown()
    }

    private func event(_ kind: String, ts: String, extra: String) -> EngineEvent {
        let line = "{\"ts\":\"\(ts)\",\"kind\":\"\(kind)\","
            + "\"project\":\"atlas\",\"section\":\"research\","
            + "\"session\":\"atlas/research/chat\",\(extra)}"
        return EngineEvent.parse(line: line, id: 0)!
    }

    private let off = QuietHours(enabled: false, startMinute: 22 * 60,
                                 endMinute: 7 * 60)

    func testSpecificConditionBodiesAndNeverOnFirstSight() {
        let coordinator = ConductorNotificationCoordinator(
            defaults: defaults, namespace: "workspace")
        XCTAssertTrue(coordinator.process(
            eventsByProject: ["atlas/research/chat": []], quietHours: off).isEmpty)

        let approval = event("approval_needed", ts: "2026-07-20T10:00:00Z",
            extra: "\"route_id\":\"r-17\",\"target\":\"planning\","
                + "\"reason\":\"target capabilities exceed workspace-only\"")
        let stalled = event("stalled", ts: "2026-07-20T10:01:00Z",
                            extra: "\"reason\":\"vote_undecided\"")
        let converged = event("converged", ts: "2026-07-20T10:02:00Z",
            extra: "\"reason\":\"no new final artifacts for 2 idle cycles\"")
        let budget = event("budget_exhausted", ts: "2026-07-20T10:03:00Z",
            extra: "\"budget\":\"spend_exhausted\","
                + "\"measurement\":\"openai $9.5 / $9.0\"")
        let output = coordinator.process(eventsByProject: [
            "atlas/research/chat": [approval, stalled, converged, budget]
        ], quietHours: off)
        XCTAssertEqual(4, output.count)
        XCTAssertTrue(output[0].body.contains("atlas / research"))
        XCTAssertTrue(output[0].body.contains("r-17"))
        XCTAssertTrue(output[0].body.contains("planning"))
        XCTAssertTrue(output[1].body.contains("vote_undecided"))
        XCTAssertTrue(output[2].body.contains("2 idle cycles"))
        XCTAssertTrue(output[3].body.contains("$9.5 / $9.0"))
    }

    func testRepeatedPassAndRelaunchDoNotRenotify() {
        let source = "atlas/research/chat"
        var coordinator = ConductorNotificationCoordinator(
            defaults: defaults, namespace: "workspace")
        XCTAssertTrue(coordinator.process(eventsByProject: [source: []],
                                          quietHours: off).isEmpty)
        let notice = event("stalled", ts: "2026-07-20T10:01:00Z",
                           extra: "\"reason\":\"vote_undecided\"")
        XCTAssertEqual(1, coordinator.process(eventsByProject: [source: [notice]],
                                              quietHours: off).count)
        XCTAssertTrue(coordinator.process(eventsByProject: [source: [notice]],
                                          quietHours: off).isEmpty)
        coordinator = ConductorNotificationCoordinator(
            defaults: defaults, namespace: "workspace")
        XCTAssertTrue(coordinator.process(eventsByProject: [source: [notice]],
                                          quietHours: off).isEmpty)
    }

    func testQuietWindowQueuesZeroPostsThenOnePersistedSummary() {
        let source = "atlas/research/chat"
        var coordinator = ConductorNotificationCoordinator(
            defaults: defaults, namespace: "workspace")
        _ = coordinator.process(eventsByProject: [source: []], quietHours: off)
        let approval = event("approval_needed", ts: "2026-07-20T23:00:00Z",
            extra: "\"route_id\":\"r-1\",\"target\":\"planning\","
                + "\"reason\":\"gated\"")
        let converged = event("converged", ts: "2026-07-20T23:01:00Z",
                              extra: "\"reason\":\"2 idle cycles\"")
        let quiet = QuietHours(enabled: true, startMinute: 22 * 60,
                               endMinute: 7 * 60)
        let atNight = Calendar.current.date(bySettingHour: 23, minute: 30,
                                            second: 0, of: Date())!
        XCTAssertTrue(coordinator.process(eventsByProject: [source: [approval, converged]],
                                          quietHours: quiet, now: atNight).isEmpty)

        coordinator = ConductorNotificationCoordinator(
            defaults: defaults, namespace: "workspace")
        let morning = Calendar.current.date(bySettingHour: 8, minute: 0,
                                            second: 0, of: Date())!
        let summary = coordinator.process(eventsByProject: [source: [approval, converged]],
                                          quietHours: off, now: morning)
        XCTAssertEqual(1, summary.count)
        guard let only = summary.only else {
            return XCTFail("quiet-hours exit must produce exactly one summary")
        }
        XCTAssertEqual("quiet_summary", only.kind)
        XCTAssertTrue(only.body.contains("1 approval waiting"))
        XCTAssertTrue(only.body.contains("1 section converged"))
        XCTAssertTrue(coordinator.process(eventsByProject: [source: [approval, converged]],
                                          quietHours: off, now: morning).isEmpty)
    }

    func testQuietHoursCrossMidnightAndEmptyQueueNoOp() {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let quiet = QuietHours(enabled: true, startMinute: 22 * 60,
                               endMinute: 7 * 60)
        let night = ISO8601DateFormatter().date(from: "2026-07-20T23:30:00Z")!
        let morning = ISO8601DateFormatter().date(from: "2026-07-21T08:00:00Z")!
        XCTAssertTrue(quiet.contains(night, calendar: calendar))
        XCTAssertFalse(quiet.contains(morning, calendar: calendar))
        let coordinator = ConductorNotificationCoordinator(
            defaults: defaults, namespace: "empty")
        XCTAssertTrue(coordinator.process(eventsByProject: [:], quietHours: off,
                                          now: morning).isEmpty)
    }

    func testScannerIncludesWorkspaceEventAndSkipsCorruptTail() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("notify-scan-\(UUID().uuidString)")
        let dir = root.appendingPathComponent(".conductor")
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try "{corrupt\n{\"ts\":\"2026-07-20T10:00:00Z\","
            .appending("\"kind\":\"budget_exhausted\","
                       + "\"budget\":\"turns_exhausted\"}\n")
            .write(to: dir.appendingPathComponent("events.jsonl"),
                   atomically: true, encoding: .utf8)
        let first = EventsScanner.scan(rootURL: root, names: [])
        XCTAssertEqual("budget_exhausted", first[".conductor"]?.only?.kind)
        let second = EventsScanner.scan(rootURL: root, names: [])
        XCTAssertEqual(first, second, "unchanged mtime+size must use cached events")
    }
}

private extension Array {
    var only: Element? { count == 1 ? first : nil }
}
