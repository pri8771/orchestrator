import XCTest
@testable import OrchestratorGUI

// Confirms TranscriptParser's normal-case behavior is unchanged now that
// headerRegex is `try?` instead of `try!` (Item 2, round 5): a nil regex
// must degrade parsing gracefully rather than crash, and the ordinary case
// (a compiled regex) must keep working exactly as before.
final class TranscriptParserTests: XCTestCase {
    private let sample = """
    # myapp — Spec

    ## Original Prompt
    ```
    build a todo app
    ```

    ## Phase Purpose
    Agree on the spec.

    ## Transcript

    ### Round 1

    **Codex — Round 1**

    Here is my proposal.

    From Codex

    **Claude — Round 1**

    I agree with Codex.

    From Claude
    """

    func testNormalCaseParsesHeadersAndMessages() {
        let t = TranscriptParser.parse(sample)
        XCTAssertTrue(t.exists)
        XCTAssertEqual(t.originalPrompt, "build a todo app")
        XCTAssertEqual(t.purpose, "Agree on the spec.")
        XCTAssertEqual(t.messages.count, 2)
        XCTAssertEqual(t.messages[0].speaker, .codex)
        XCTAssertEqual(t.messages[0].body, "Here is my proposal.")
        XCTAssertEqual(t.messages[1].speaker, .claude)
        XCTAssertEqual(t.messages[1].body, "I agree with Codex.")
    }

    // No "## Transcript" heading at all: parse() must return early with the
    // metadata it found and no messages, never throw or crash.
    func testMissingTranscriptSectionDegradesGracefully() {
        let text = "# app — Title\n\n## Phase Purpose\nSomething.\n"
        let t = TranscriptParser.parse(text)
        XCTAssertTrue(t.exists)
        XCTAssertEqual(t.purpose, "Something.")
        XCTAssertTrue(t.messages.isEmpty)
    }

    // Empty input is the degenerate case of a fail-open parser: no crash,
    // an empty-but-valid transcript back.
    func testEmptyInputDoesNotCrash() {
        let t = TranscriptParser.parse("")
        XCTAssertTrue(t.exists)
        XCTAssertTrue(t.messages.isEmpty)
    }

    func testChatMetaCommentNeverChangesRenderedTranscript() {
        let plain = TranscriptParser.parse(sample)
        let withMeta = TranscriptParser.parse(
            "<!-- chat-meta: {\"pinned\":true,\"tags\":[\"pricing\"]} -->\n" + sample)
        XCTAssertEqual(withMeta, plain)

        let inside = sample.replacingOccurrences(
            of: "Here is my proposal.",
            with: "<!-- chat-meta: {\"pinned\":true,\"tags\":[]} -->\nHere is my proposal.")
        XCTAssertEqual(TranscriptParser.parse(inside), plain)
    }
}
