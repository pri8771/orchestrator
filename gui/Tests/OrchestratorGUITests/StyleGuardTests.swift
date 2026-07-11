import XCTest

// Native Pro M0 guardrail: runs the repo's CI style grep as part of the test
// suite, so `swift test` rejects `Color(red:` and hardcoded font sizes outside
// ThemeTokens.swift (DESIGN-NATIVE-PRO.md §2/§9).
final class StyleGuardTests: XCTestCase {

    func testNoRogueColorsOrFontSizes() throws {
        // Tests/OrchestratorGUITests/StyleGuardTests.swift → gui/
        let guiDir = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // OrchestratorGUITests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // gui
        let script = guiDir.appendingPathComponent("ci_style_check.sh")
        guard FileManager.default.fileExists(atPath: script.path) else {
            throw XCTSkip("ci_style_check.sh not found (built outside the repo checkout)")
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = [script.path]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        try proc.run()
        proc.waitUntilExit()
        let output = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(),
                            as: UTF8.self)
        XCTAssertEqual(proc.terminationStatus, 0,
                       "Style guard failed:\n\(output)")
    }
}
