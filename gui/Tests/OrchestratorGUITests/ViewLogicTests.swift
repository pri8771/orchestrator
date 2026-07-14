import XCTest
@testable import OrchestratorGUI

// Pure, view-adjacent logic that has no other test coverage (Item 3, round 5
// audit): command-palette fuzzy matching, download-count formatting, and
// Project's derived status strings. Full SwiftUI view rendering is a
// deliberate non-goal here — see DESIGN-REFRESH.md — this file targets the
// pure functions/computed properties that feed those views instead.
final class ViewLogicTests: XCTestCase {

    // MARK: - CommandPaletteView.fuzzyScore

    func testFuzzyScoreEmptyQueryMatchesEverything() {
        XCTAssertEqual(CommandPaletteView.fuzzyScore("", "anything"), 0)
    }

    func testFuzzyScorePrefixRanksHighest() {
        XCTAssertEqual(CommandPaletteView.fuzzyScore("bui", "Build an App"), 3)
    }

    func testFuzzyScoreSubstringRanksMiddle() {
        XCTAssertEqual(CommandPaletteView.fuzzyScore("an app", "Build an App"), 2)
    }

    func testFuzzyScoreOrderedSubsequenceRanksLowest() {
        // "bap" -> B(uild an A)pp: in-order subsequence but not contiguous.
        XCTAssertEqual(CommandPaletteView.fuzzyScore("bap", "Build an App"), 1)
    }

    func testFuzzyScoreOutOfOrderDoesNotMatch() {
        // "pab" is a subsequence of the reversed order only — never in-order.
        XCTAssertNil(CommandPaletteView.fuzzyScore("pab", "Build an App"))
    }

    func testFuzzyScoreNoMatchReturnsNil() {
        XCTAssertNil(CommandPaletteView.fuzzyScore("zzz", "Build an App"))
    }

    func testFuzzyScoreIsCaseInsensitive() {
        XCTAssertEqual(CommandPaletteView.fuzzyScore("BUILD", "build an app"), 3)
    }

    // MARK: - compactCount

    func testCompactCountBelowThousandIsRaw() {
        XCTAssertEqual(compactCount(0), "0")
        XCTAssertEqual(compactCount(999), "999")
    }

    func testCompactCountThousands() {
        XCTAssertEqual(compactCount(1_000), "1k")
        XCTAssertEqual(compactCount(51_000), "51k")
        XCTAssertEqual(compactCount(999_999), "1000k")
    }

    func testCompactCountMillions() {
        XCTAssertEqual(compactCount(2_400_000), "2.4M")
        XCTAssertEqual(compactCount(1_000_000), "1.0M")
    }

    // MARK: - Project.progressText / phaseStatus

    private func makeProject(status: ProjectStatus, currentPhase: String? = nil,
                             currentRound: Int = 1, completedPhases: [String] = [],
                             phaseCount: Int = 5) -> Project {
        Project(name: "demo", status: status, currentPhase: currentPhase,
               currentRound: currentRound, nextAgent: nil, error: nil,
               lastProcessed: nil, completedPhases: completedPhases,
               phaseOutputs: [:], dirURL: URL(fileURLWithPath: "/tmp/demo"),
               phaseCount: phaseCount)
    }

    func testProgressTextNew() {
        XCTAssertEqual(makeProject(status: .new).progressText, "not started")
    }

    func testProgressTextDone() {
        let p = makeProject(status: .done, completedPhases: ["spec", "build"], phaseCount: 5)
        XCTAssertEqual(p.progressText, "2/5 phases")
    }

    func testProgressTextAbortedWithPhase() {
        let p = makeProject(status: .aborted, currentPhase: "spec")
        XCTAssertEqual(p.progressText, "stopped at \(p.titleFor("spec"))")
    }

    func testProgressTextAbortedWithoutPhase() {
        XCTAssertEqual(makeProject(status: .aborted).progressText, "aborted")
    }

    func testProgressTextInProgressWithPhase() {
        let p = makeProject(status: .inProgress, currentPhase: "spec", currentRound: 2)
        XCTAssertEqual(p.progressText, "\(p.titleFor("spec")) · round 2")
    }

    func testProgressTextInProgressWithoutPhaseFallsBackToPhaseCount() {
        let p = makeProject(status: .inProgress, completedPhases: ["spec"], phaseCount: 4)
        XCTAssertEqual(p.progressText, "1/4 phases")
    }

    func testPhaseStatusDoneTakesPriorityOverCurrent() {
        let p = makeProject(status: .inProgress, currentPhase: "spec",
                            completedPhases: ["spec"])
        XCTAssertEqual(p.phaseStatus("spec"), .done)
    }

    func testPhaseStatusActiveWhenCurrentAndNotAborted() {
        let p = makeProject(status: .inProgress, currentPhase: "spec")
        XCTAssertEqual(p.phaseStatus("spec"), .active)
    }

    func testPhaseStatusAbortedWhenCurrentAndAborted() {
        let p = makeProject(status: .aborted, currentPhase: "spec")
        XCTAssertEqual(p.phaseStatus("spec"), .aborted)
    }

    func testPhaseStatusPendingOtherwise() {
        let p = makeProject(status: .inProgress, currentPhase: "spec")
        XCTAssertEqual(p.phaseStatus("build"), .pending)
    }
}
