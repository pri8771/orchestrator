import XCTest
@testable import OrchestratorGUI

// V3 board 1.6: conversational transcript parsing + chat-surface derivations.
final class ChatSurfaceTests: XCTestCase {

    private let conversational = """
    # home--ideas--demo — Ideas Chat

    ## Original Prompt

    ```
    Brainstorm a weekend-sized iOS utility.
    ```

    ## Transcript

    ### Round 1

    **You (human) — Round 1**

    What's a tiny iOS utility I could ship this weekend?

    **Codex (Pragmatist) — Round 1**

    A menu-bar water tracker.

    From Codex

    **Local (Ollama) — Round 1**

    Cheap second opinion: the reminder angle differentiates.

    **Gemini — Round 1 (skipped: CLI unavailable)**

    _not logged in_

    ## Coordinator Decision

    _No coordinator — conversational phase; ended by user._

    ## Final Output

    Conversation closed after 1 round(s): ended by user.

    ---

    ENDED BY USER
    """

    func testHumanBlocksParseAsHumanSpeaker() {
        let t = TranscriptParser.parse(conversational)
        let human = t.messages.filter { $0.speaker == .human }
        XCTAssertEqual(human.count, 1)
        XCTAssertTrue(human[0].body.contains("tiny iOS utility"))
    }

    func testLocalOllamaTurnIsItsOwnBubbleNotAbsorbed() {
        // Review finding: '**Local (Ollama) — Round N**' previously failed
        // isHeader and the whole turn merged into the previous bubble.
        let t = TranscriptParser.parse(conversational)
        let local = t.messages.filter { $0.speaker == .ollama }
        XCTAssertEqual(local.count, 1, "local turn must be a distinct bubble")
        XCTAssertTrue(local[0].body.contains("second opinion"))
        // And Codex's bubble did NOT swallow it.
        let codex = t.messages.first { $0.speaker == .codex }
        XCTAssertEqual(codex?.body.contains("second opinion"), false)
    }

    func testSkippedTurnAnnotationIsNotAPersonaChip() {
        let t = TranscriptParser.parse(conversational)
        let skipped = t.messages.first { $0.header.contains("skipped") }
        XCTAssertNotNil(skipped)
        XCTAssertEqual(skipped?.persona, "",
                       "'skipped: CLI unavailable' must never render as a persona")
        // A real persona still parses.
        let codex = t.messages.first { $0.speaker == .codex }
        XCTAssertEqual(codex?.persona, "Pragmatist")
    }

    func testNoCoordinatorBubbleButHeadingTerminatesTranscript() {
        let t = TranscriptParser.parse(conversational)
        // The literal '## Coordinator Decision' heading legitimately exists
        // (it terminates the message stream); a Coordinator message BUBBLE
        // must not.
        XCTAssertFalse(t.messages.contains { $0.speaker == .coordinator })
        XCTAssertTrue(t.finalOutput?.contains("ended by user") == true)
        // ENDED BY USER is not a known marker — the chat surface renders its
        // own ended treatment instead of a MarkerBadge.
        XCTAssertNil(t.marker)
    }

    // MARK: surface derivations

    func testComposerModes() {
        XCTAssertEqual(ChatSurfaceLogic.composerMode(for: .running), .live)
        XCTAssertEqual(ChatSurfaceLogic.composerMode(for: .waitingForHuman), .live)
        if case .queued = ChatSurfaceLogic.composerMode(for: .stopped) {} else {
            XCTFail("stopped must queue — the round-open drain delivers on relaunch")
        }
        if case .disabled(let r) = ChatSurfaceLogic.composerMode(for: .ended(reason: "ended by user")) {
            XCTAssertTrue(r.contains("ended"))
        } else { XCTFail("ended must disable the composer with a reason") }
        if case .disabled(let r) = ChatSurfaceLogic.composerMode(
            for: .crashed(code: 9, wasSignal: true)) {
            XCTAssertTrue(r.contains("signal 9"), "signal deaths named as signals (§5.2)")
        } else { XCTFail("crashed must disable the composer with a reason") }
    }

    func testReplyingShimmerOnlyWhileGenuinelyMidRound() {
        XCTAssertTrue(ChatSurfaceLogic.showsReplying(state: .running,
                                                     nextAgent: "codex+claude+gemini"))
        XCTAssertFalse(ChatSurfaceLogic.showsReplying(state: .running, nextAgent: nil))
        XCTAssertFalse(ChatSurfaceLogic.showsReplying(state: .waitingForHuman,
                                                      nextAgent: "codex+claude"))
        XCTAssertFalse(ChatSurfaceLogic.showsReplying(state: .stopped,
                                                      nextAgent: "codex"))
        XCTAssertFalse(ChatSurfaceLogic.showsReplying(
            state: .crashed(code: 1, wasSignal: false), nextAgent: "codex"))
    }

    func testWaitingAffordanceOnlyInWaitingState() {
        XCTAssertTrue(ChatSurfaceLogic.showsWaitingForYou(state: .waitingForHuman))
        for s in [ChatSessionState.running, .stopped, .idle,
                  .ended(reason: "ended by user")] {
            XCTAssertFalse(ChatSurfaceLogic.showsWaitingForYou(state: s))
        }
    }
}
