import XCTest
@testable import OrchestratorGUI

// surfaceError() itself needs a live OrchestratorStore (MainActor, touches
// disk in init), so this covers the pure formatting it delegates to:
// OrchestratorStore.formatSurfacedError(). The ~8 call sites all rely on
// this same trim/newline behavior for the run-log line and banner text.
final class SurfaceErrorTests: XCTestCase {

    func testAppendsNewlineWhenMissing() {
        let (logLine, _) = OrchestratorStore.formatSurfacedError("Could not save settings.")
        XCTAssertEqual(logLine, "Could not save settings.\n")
    }

    func testDoesNotDoubleNewline() {
        let (logLine, _) = OrchestratorStore.formatSurfacedError("Could not save settings.\n")
        XCTAssertEqual(logLine, "Could not save settings.\n")
    }

    func testBannerIsTrimmedOfWhitespaceAndNewlines() {
        let (_, banner) = OrchestratorStore.formatSurfacedError("  Could not save settings.  \n")
        XCTAssertEqual(banner, "Could not save settings.")
    }

    func testBannerPreservesInternalContent() {
        let (_, banner) = OrchestratorStore.formatSurfacedError(
            "Could not queue your message for MyApp: disk full")
        XCTAssertEqual(banner, "Could not queue your message for MyApp: disk full")
    }
}
