import XCTest
@testable import OrchestratorGUI

/// RunSuggestion.parse: the ```run-json``` fence a concierge reply may carry.
/// Previously untested; the edge cases here are the ones models actually
/// produce (missing fence, unterminated fence, malformed JSON, empty prompt).
final class RunSuggestionTests: XCTestCase {

    func testWellFormedFenceParsesAndIsRemovedFromProse() {
        let reply = """
        Great — here's what I'd run.
        ```run-json
        {"name": "Habit Tracker", "workflow": "app_build", "prompt": "Build a habit tracker"}
        ```
        Anything else?
        """
        let (clean, s) = RunSuggestion.parse(from: reply)
        XCTAssertEqual(s?.name, "Habit Tracker")
        XCTAssertEqual(s?.workflow, "app_build")
        XCTAssertEqual(s?.prompt, "Build a habit tracker")
        XCTAssertFalse(clean.contains("run-json"))
        XCTAssertTrue(clean.contains("Great"))
        XCTAssertTrue(clean.contains("Anything else?"))
    }

    func testNoFenceReturnsReplyUnchanged() {
        let (clean, s) = RunSuggestion.parse(from: "Just a normal answer.")
        XCTAssertNil(s)
        XCTAssertEqual(clean, "Just a normal answer.")
    }

    func testUnterminatedFenceIsLeftAlone() {
        let reply = "Here:\n```run-json\n{\"prompt\": \"x\""
        let (clean, s) = RunSuggestion.parse(from: reply)
        XCTAssertNil(s)
        XCTAssertEqual(clean, reply)
    }

    func testMalformedJSONStripsFenceButYieldsNoSuggestion() {
        let reply = "Try this.\n```run-json\n{not json}\n```"
        let (clean, s) = RunSuggestion.parse(from: reply)
        XCTAssertNil(s)
        XCTAssertFalse(clean.contains("run-json"))
    }

    func testEmptyPromptYieldsNoSuggestion() {
        let reply = "```run-json\n{\"prompt\": \"\", \"workflow\": \"sprint\"}\n```"
        let (_, s) = RunSuggestion.parse(from: reply)
        XCTAssertNil(s)
    }

    func testMissingNameAndWorkflowUseDefaults() {
        let reply = "```run-json\n{\"prompt\": \"Build a tip splitter\"}\n```"
        let (_, s) = RunSuggestion.parse(from: reply)
        XCTAssertEqual(s?.name, "")
        XCTAssertEqual(s?.workflow, "app_build")
        XCTAssertEqual(s?.prompt, "Build a tip splitter")
    }
}
