import XCTest
@testable import OrchestratorGUI

// OrchestratorStore.cliSearchDirs() is a pure static function (no store
// instance needed), unlike detectCLIs/ollamaOnPath which also touch the
// filesystem — this covers the PATH parsing/dedup logic directly.
final class CliSearchDirsTests: XCTestCase {

    func testNoDuplicateDirectories() {
        let dirs = OrchestratorStore.cliSearchDirs()
        XCTAssertEqual(dirs.count, Set(dirs).count, "cliSearchDirs() returned duplicates")
    }

    func testNoEmptyEntries() {
        let dirs = OrchestratorStore.cliSearchDirs()
        XCTAssertFalse(dirs.contains(""), "an empty PATH component must be filtered out")
    }

    func testIncludesKnownExtraLocations() {
        let dirs = OrchestratorStore.cliSearchDirs()
        XCTAssertTrue(dirs.contains("/usr/local/bin"))
        XCTAssertTrue(dirs.contains("/opt/homebrew/bin"))
    }

    func testPreservesPathOrderBeforeExtras() {
        // Directories from $PATH should be searched before the hardcoded
        // extras, so a user's PATH override still takes precedence.
        let dirs = OrchestratorStore.cliSearchDirs()
        if let pathValue = ProcessInfo.processInfo.environment["PATH"],
           let firstPathDir = pathValue.split(separator: ":").first {
            XCTAssertEqual(dirs.first, String(firstPathDir))
        }
    }
}
