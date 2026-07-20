import XCTest
@testable import OrchestratorGUI

// V3 6.4: the cost meter's three honest states and the costs.jsonl scanner.
// Honesty invariants: unmetered never renders $0.00; a mixed scope renders
// ">= $X · N unmetered", never a bare total; rounding only at display.
final class CostMeterTests: XCTestCase {

    func testFullyMeteredRendersBareDollars() {
        var t = CostTotals()
        t.fold(metered: true, cost: 1_230_000)
        t.fold(metered: true, cost: 2_000_000)
        XCTAssertEqual(t.display, "$3.23")
    }

    func testMixedRendersFloorWithUnmeteredCount() {
        var t = CostTotals()
        t.fold(metered: true, cost: 5_000_000)
        t.fold(metered: false, cost: nil)          // CLI turn
        t.fold(metered: true, cost: nil)           // metered, unpriced model
        XCTAssertEqual(t.display, "≥ $5.00 · 2 unmetered")
    }

    func testNoRealDollarsRendersTheWordNeverZero() {
        var cli = CostTotals()
        cli.fold(metered: false, cost: nil)
        XCTAssertEqual(cli.display, "unmetered")
        var unpriced = CostTotals()
        unpriced.fold(metered: true, cost: nil)
        XCTAssertEqual(unpriced.display, "unmetered")
        XCTAssertFalse(cli.display?.contains("$0") ?? true)
    }

    func testEmptyScopeRendersNothing() {
        XCTAssertNil(CostTotals().display)
    }

    func testScannerAggregatesPerAgentAndSkipsMalformed() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let proj = root.appendingPathComponent("projA")
        try FileManager.default.createDirectory(at: proj,
                                                withIntermediateDirectories: true)
        let lines = [
            #"{"v":1,"agent":"api:anthropic:h","metered":true,"cost_micro_usd":100}"#,
            #"{"v":1,"agent":"api:anthropic:h","provider":"anthropic","metered":true,"cost_micro_usd":200}"#,
            #"{"v":1,"agent":"claude","metered":false,"cost_micro_usd":null}"#,
            "not json at all",
            "[1,2,3]",
        ].joined(separator: "\n")
        try lines.write(to: proj.appendingPathComponent("costs.jsonl"),
                        atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: root) }

        let scan = CostsScanner.scan(rootURL: root, names: ["projA", "absent"])
        let costs = try XCTUnwrap(scan["projA"])
        XCTAssertNil(scan["absent"])
        XCTAssertEqual(costs.total.meteredTurns, 2)
        XCTAssertEqual(costs.total.unmeteredTurns, 1)
        XCTAssertEqual(costs.total.costMicroUSD, 300)
        XCTAssertEqual(costs.byAgent["api:anthropic:h"]?.costMicroUSD, 300)
        XCTAssertEqual(costs.byAgent["claude"]?.unmeteredTurns, 1)
        XCTAssertEqual(costs.byProvider["anthropic"]?.costMicroUSD, 200)
    }

    func testOverCapFileServesLastKnownTotalsNotNothing() throws {
        // costs.jsonl is append-only: once past the cap it never shrinks, so
        // a hard skip would permanently vanish the meter. The scanner must
        // fall back to the last parsed totals (a stale-but-honest floor).
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let proj = root.appendingPathComponent("bigproj")
        try FileManager.default.createDirectory(at: proj,
                                                withIntermediateDirectories: true)
        let url = proj.appendingPathComponent("costs.jsonl")
        let line = #"{"v":1,"agent":"a","metered":true,"cost_micro_usd":10}"# + "\n"
        try line.write(to: url, atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: root) }

        let before = CostsScanner.scan(rootURL: root, names: ["bigproj"])
        XCTAssertEqual(before["bigproj"]?.total.costMicroUSD, 10)

        // Push the file past the cap (append-only growth).
        let filler = String(repeating: line,
                            count: CostsScanner.maxBytes / line.utf8.count + 2)
        let fh = try FileHandle(forWritingTo: url)
        try fh.seekToEnd()
        try fh.write(contentsOf: Data(filler.utf8))
        try fh.close()

        let after = CostsScanner.scan(rootURL: root, names: ["bigproj"])
        XCTAssertEqual(after["bigproj"]?.total.costMicroUSD, 10,
                       "over-cap file must serve last-known totals, not vanish")
    }

    func testScannerCacheServesUnchangedFiles() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        let proj = root.appendingPathComponent("projB")
        try FileManager.default.createDirectory(at: proj,
                                                withIntermediateDirectories: true)
        try #"{"v":1,"agent":"a","metered":true,"cost_micro_usd":7}"#
            .write(to: proj.appendingPathComponent("costs.jsonl"),
                   atomically: true, encoding: .utf8)
        defer { try? FileManager.default.removeItem(at: root) }
        let first = CostsScanner.scan(rootURL: root, names: ["projB"])
        let second = CostsScanner.scan(rootURL: root, names: ["projB"])
        XCTAssertEqual(first, second)
        XCTAssertEqual(second["projB"]?.total.costMicroUSD, 7)
    }
}
