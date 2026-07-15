import XCTest
@testable import OrchestratorGUI

/// FactoryScanner.scanLocks: parses <root>/.orch-locks/<app>.lock, the payload
/// the engine writes for both GUI-launched and externally-launched (shepherd,
/// terminal) runs — this is what lets the GUI Stop button signal a run it
/// didn't spawn. Previously untested.
final class FactoryScannerLockTests: XCTestCase {

    private var root: URL!

    override func setUp() {
        super.setUp()
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(
            at: root.appendingPathComponent(".orch-locks"),
            withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: root)
        super.tearDown()
    }

    private func writeLock(_ app: String, _ body: String) {
        let url = root.appendingPathComponent(".orch-locks/\(app).lock")
        try? body.write(to: url, atomically: true, encoding: .utf8)
    }

    func testParsesPidAndStartedStamp() {
        writeLock("demo", "pid=4242 host=box started=2026-07-15 09:30:00\n")
        let locks = FactoryScanner.scanLocks(rootURL: root)
        XCTAssertEqual(locks["demo"]?.pid, 4242)
        let comps = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute], from: locks["demo"]!.since)
        XCTAssertEqual(comps.hour, 9)
        XCTAssertEqual(comps.minute, 30)
    }

    func testMissingPidFieldYieldsNilPid() {
        writeLock("demo", "host=box started=2026-07-15 09:30:00\n")
        let locks = FactoryScanner.scanLocks(rootURL: root)
        XCTAssertNil(locks["demo"]?.pid)
    }

    func testNoLocksDirectoryReturnsEmpty() {
        let missingRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        XCTAssertTrue(FactoryScanner.scanLocks(rootURL: missingRoot).isEmpty)
    }

    func testNonLockFilesAreIgnored() {
        try? "not a lock".write(
            to: root.appendingPathComponent(".orch-locks/readme.txt"),
            atomically: true, encoding: .utf8)
        XCTAssertTrue(FactoryScanner.scanLocks(rootURL: root).isEmpty)
    }

    func testMultipleAppsParsedIndependently() {
        writeLock("alpha", "pid=100 host=box started=2026-07-15 08:00:00\n")
        writeLock("beta", "pid=200 host=box started=2026-07-15 08:05:00\n")
        let locks = FactoryScanner.scanLocks(rootURL: root)
        XCTAssertEqual(locks["alpha"]?.pid, 100)
        XCTAssertEqual(locks["beta"]?.pid, 200)
    }
}
