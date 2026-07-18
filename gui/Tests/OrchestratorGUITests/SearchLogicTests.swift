import XCTest
@testable import OrchestratorGUI

// V3 board 2.6 (GUI): the pure search plumbing — parsing of the frozen
// search.py --json contract, degraded-status surfacing, palette row
// details, and anchor→message matching. No OrchestratorStore is ever
// instantiated (it writes real user data).
final class SearchLogicTests: XCTestCase {

    private func data(_ s: String) -> Data { Data(s.utf8) }

    func testParsesTheFrozenContract() {
        let payload = """
        {"status": "ok", "hits": [{"project": "gloam", "phase": "design",
          "round": 2, "agent": "codex", "kind": "turn",
          "turn_id": "design:2:codex:turn",
          "content_path": "design/d.md", "snippet": "the kraken…"}]}
        """
        let res = SearchResultParser.parse(data(payload))
        XCTAssertEqual(res?.status, "ok")
        XCTAssertEqual(res?.hits.count, 1)
        let h = res!.hits[0]
        XCTAssertEqual(h.project, "gloam")
        XCTAssertEqual(h.turnId, "design:2:codex:turn")
        XCTAssertEqual(h.round, 2)
        XCTAssertEqual(h.id, "gloam|design:2:codex:turn")
    }

    func testMalformedPayloadIsNilNotEmpty() {
        // nil = "the query failed" — the caller surfaces a degraded state;
        // an empty hits array would silently read as "no matches" (R2).
        XCTAssertNil(SearchResultParser.parse(data("not json")))
        XCTAssertNil(SearchResultParser.parse(data("{\"hits\": []}")))
        XCTAssertNotNil(SearchResultParser.parse(
            data("{\"status\": \"ok\", \"hits\": []}")))
    }

    func testHitMissingRequiredFieldsIsSkippedNotFatal() {
        let payload = """
        {"status": "ok", "hits": [{"phase": "design"},
          {"project": "gloam", "turn_id": "design:1:codex:turn"}]}
        """
        let res = SearchResultParser.parse(data(payload))
        XCTAssertEqual(res?.hits.map(\.turnId), ["design:1:codex:turn"])
    }

    func testDegradedDetection() {
        XCTAssertTrue(SearchResultParser.isDegraded("degraded:fts5-unavailable"))
        XCTAssertTrue(SearchResultParser.isDegraded("degraded:search-unavailable"))
        XCTAssertFalse(SearchResultParser.isDegraded("ok"))
    }

    func testDetailLine() {
        let h = SearchHit(project: "gloam", phase: "design", round: 2,
                          agent: "codex", kind: "turn",
                          turnId: "design:2:codex:turn",
                          contentPath: "design/d.md", snippet: "s")
        XCTAssertEqual(SearchResultParser.detail(for: h),
                       "gloam · design · r2 · codex")
        let final = SearchHit(project: "gloam", phase: "design", round: 0,
                              agent: "", kind: "tally",
                              turnId: "design:final:orchestrator:tally",
                              contentPath: "design/d.md", snippet: "s")
        XCTAssertEqual(SearchResultParser.detail(for: final),
                       "gloam · design")
    }

    private func msg(_ id: Int, section: String, header: String) -> ChatMessage {
        ChatMessage(id: id, speaker: .codex, header: header,
                    section: section, body: "b")
    }

    func testAnchorMatchesAgentWithinRound() {
        let messages = [
            msg(0, section: "Round 1", header: "Codex — Round 1"),
            msg(1, section: "Round 2", header: "Claude — Round 2"),
            msg(2, section: "Round 2", header: "Codex — Round 2"),
        ]
        let anchor = TranscriptAnchor(project: "gloam", phase: "design",
                                      round: 2, agent: "codex")
        XCTAssertEqual(SearchAnchorLogic.messageID(for: anchor,
                                                   in: messages), 2)
    }

    func testAnchorFallsBackToSectionFirstThenNil() {
        let messages = [
            msg(0, section: "Round 1", header: "Codex — Round 1"),
            msg(1, section: "Round 2", header: "Claude — Round 2"),
        ]
        let unknownAgent = TranscriptAnchor(project: "g", phase: "d",
                                            round: 2, agent: "gemini")
        XCTAssertEqual(SearchAnchorLogic.messageID(for: unknownAgent,
                                                   in: messages), 1)
        let missingRound = TranscriptAnchor(project: "g", phase: "d",
                                            round: 9, agent: "codex")
        XCTAssertNil(SearchAnchorLogic.messageID(for: missingRound,
                                                 in: messages),
                     "an absent round must not scroll anywhere")
        let finalStage = TranscriptAnchor(project: "g", phase: "d",
                                          round: 0, agent: "codex")
        XCTAssertNil(SearchAnchorLogic.messageID(for: finalStage,
                                                 in: messages))
    }
}
