import XCTest
@testable import OrchestratorGUI

/// The small live log under ParallelBuildBanner: filterBuildActivity is a
/// pure function of eventsByProject, so it's tested directly — no store or
/// project fixture needed.
final class BuildActivityLogTests: XCTestCase {
    private func event(kind: String, phase: String = "prompt_contract",
                       round: Int = 1, agent: String = "codex",
                       id: Int = 0) -> EngineEvent {
        EngineEvent(id: id, ts: Date(), kind: kind, phase: phase,
                   round: round, agent: agent)
    }

    func testKeepsOnlyTurnAndFallbackKindsForThisPhaseAndRound() {
        let events = [
            event(kind: "turn_started", id: 0),
            event(kind: "phase_started", id: 1),   // excluded kind
            event(kind: "turn_completed", id: 2),
            event(kind: "agent_fallback", id: 3),
            event(kind: "run_finished", id: 4),    // excluded kind
        ]
        let kept = filterBuildActivity(events, phase: "prompt_contract",
                                       round: 1, limit: 10)
        XCTAssertEqual(kept.map(\.id), [3, 2, 0])   // newest first
    }

    func testExcludesEventsFromAnEarlierRound() {
        let events = [
            event(kind: "turn_completed", round: 1, id: 0),
            event(kind: "turn_completed", round: 2, id: 1),
        ]
        let kept = filterBuildActivity(events, phase: "prompt_contract",
                                       round: 2, limit: 10)
        XCTAssertEqual(kept.map(\.id), [1])
    }

    func testExcludesEventsFromADifferentPhase() {
        let events = [
            event(kind: "turn_completed", phase: "prompt_contract", id: 0),
            event(kind: "turn_completed", phase: "tech_specs", id: 1),
        ]
        let kept = filterBuildActivity(events, phase: "prompt_contract",
                                       round: 1, limit: 10)
        XCTAssertEqual(kept.map(\.id), [0])
    }

    func testCapsToTheLimitKeepingTheMostRecent() {
        let events = (0..<10).map { event(kind: "turn_started", id: $0) }
        let kept = filterBuildActivity(events, phase: "prompt_contract",
                                       round: 1, limit: 3)
        XCTAssertEqual(kept.map(\.id), [9, 8, 7])
    }

    func testEmptyInputYieldsEmptyOutput() {
        XCTAssertEqual(filterBuildActivity([], phase: "prompt_contract",
                                           round: 1, limit: 6), [])
    }

    func testNoMatchingEventsYieldsEmptyOutput() {
        let events = [event(kind: "phase_started"), event(kind: "run_finished")]
        XCTAssertEqual(filterBuildActivity(events, phase: "prompt_contract",
                                           round: 1, limit: 6), [])
    }
}
