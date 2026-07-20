import XCTest
@testable import OrchestratorGUI

final class PaneCanvasStateTests: XCTestCase {
    func testOnePaneThenSplitToThreeAndFourthOverflows() {
        var state = PaneCanvasState()
        state.open("one", split: false)
        XCTAssertEqual(state.panes, ["one"])
        XCTAssertEqual(state.focusedSessionID, "one")

        state.open("two", split: true)
        state.open("three", split: true)
        state.open("four", split: true)

        XCTAssertEqual(state.panes, ["one", "two", "three"])
        XCTAssertEqual(state.overflow, ["four"])
        XCTAssertEqual(state.focusedSessionID, "three")
        XCTAssertLessThanOrEqual(state.panes.count, PaneCanvasState.maximumPanes)
    }

    func testDropOntoPaneReplacesAndOverflowChipSwapsBack() {
        var state = PaneCanvasState()
        for id in ["one", "two", "three"] { state.open(id, split: true) }
        state.replace(pane: "two", with: "four")
        XCTAssertEqual(state.panes, ["one", "four", "three"])
        XCTAssertEqual(state.overflow, ["two"])
        XCTAssertEqual(state.focusedSessionID, "four")

        state.activateOverflow("two")
        XCTAssertEqual(state.panes, ["one", "two", "three"])
        XCTAssertEqual(state.overflow, ["four"])
        XCTAssertEqual(state.focusedSessionID, "two")
    }

    func testCloseWidensAndFocusAlwaysNamesAnOpenPane() {
        var state = PaneCanvasState()
        state.open("one", split: false)
        state.open("two", split: true)
        state.focus("one")
        state.closeFocused()
        XCTAssertEqual(state.panes, ["two"])
        XCTAssertEqual(state.focusedSessionID, "two")
        state.closeFocused()
        XCTAssertTrue(state.panes.isEmpty)
        XCTAssertNil(state.focusedSessionID)
    }

    func testNarrowCanvasCollapsesWithoutCreatingAClippedFourthPane() {
        var state = PaneCanvasState()
        for id in ["one", "two", "three", "four"] {
            state.open(id, split: true)
        }
        XCTAssertEqual(state.visibleCount(availableWidth: 620), 2)
        XCTAssertEqual(state.visibleCount(availableWidth: 280), 1)
        XCTAssertEqual(state.visibleCount(availableWidth: 1_000), 3)
        XCTAssertEqual(state.panes.count, 3)
        XCTAssertEqual(state.overflow.count, 1)
    }

    func testPollingTiersAreFocusedVisibleAndBackground() {
        var state = PaneCanvasState()
        state.open("focused", split: false)
        state.open("visible", split: true)
        XCTAssertEqual(state.pollingInterval(for: "visible"), 0.5)
        XCTAssertEqual(state.pollingInterval(for: "focused"), 1.5)
        XCTAssertEqual(state.pollingInterval(for: "background"), 5.0)
        XCTAssertEqual(CommandPaletteView.paneVerbActions,
                       [.focusPane1, .focusPane2, .focusPane3, .closeFocusedPane])
    }

    func testSessionDragPayloadRejectsArtifactAndPlainText() {
        XCTAssertEqual(PaneSessionDrag.decode("orchestrator-session:p/s/chat"),
                       "p/s/chat")
        XCTAssertNil(PaneSessionDrag.decode("plain text"))
        XCTAssertNil(PaneSessionDrag.decode("orchestrator-artifact:{}"))
    }
}

final class FocusedFileCacheTests: XCTestCase {
    private var temp: URL!

    override func setUpWithError() throws {
        temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("pane-cache-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: temp,
                                                withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: temp)
    }

    func testTranscriptCacheSkipsUnchangedAndDetectsSameMtimeDifferentSize() throws {
        let url = temp.appendingPathComponent("phase.md")
        try "# Phase\n".write(to: url, atomically: true, encoding: .utf8)
        var parses = 0
        let first = OrchestratorStore.readAndParseTranscript(
            at: url, ifFingerprintDiffersFrom: nil) { parses += 1 }
        XCTAssertNotNil(first.fresh)
        XCTAssertEqual(parses, 1)

        let unchanged = OrchestratorStore.readAndParseTranscript(
            at: url, ifFingerprintDiffersFrom: first.fingerprint) { parses += 1 }
        XCTAssertNil(unchanged.fresh)
        XCTAssertEqual(parses, 1, "stable mtime+size must perform no read/parse")

        try "# Phase\nA longer body\n".write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes(
            [.modificationDate: first.fingerprint.mtime], ofItemAtPath: url.path)
        let changed = OrchestratorStore.readAndParseTranscript(
            at: url, ifFingerprintDiffersFrom: first.fingerprint) { parses += 1 }
        XCTAssertNotNil(changed.fresh)
        XCTAssertNotEqual(changed.fingerprint.size, first.fingerprint.size)
        XCTAssertEqual(parses, 2)
    }

    func testFocusedProjectTickStatsButDoesNotReparseStableState() throws {
        let projectDir = temp.appendingPathComponent("app")
        try FileManager.default.createDirectory(at: projectDir,
                                                withIntermediateDirectories: true)
        let stateURL = projectDir.appendingPathComponent("agent_state.json")
        try #"{"current_phase":"initial_discussion","current_round":1}"#
            .write(to: stateURL, atomically: true, encoding: .utf8)
        let phase = PhaseDef(key: "initial_discussion",
                             folder: "initial_discussion",
                             file: "initial_discussion.md", title: "Initial")
        let workflow = WorkflowDef(name: "app_build", title: "Build",
                                   description: "", target: "app",
                                   phases: [phase])
        var parses = 0
        let first = BackgroundProjectLoader.loadProjectsCached(
            names: ["app"], rootURL: temp,
            workflowsByName: ["app_build": workflow], defaultWorkflow: workflow,
            manualStops: [:], runningProcessNames: [], cache: [:],
            dueIntervals: ["app": 0.5], now: Date()) { _ in parses += 1 }
        XCTAssertEqual(parses, 1)

        let second = BackgroundProjectLoader.loadProjectsCached(
            names: ["app"], rootURL: temp,
            workflowsByName: ["app_build": workflow], defaultWorkflow: workflow,
            manualStops: [:], runningProcessNames: [], cache: first.cache,
            dueIntervals: ["app": 0.5], now: Date().addingTimeInterval(1)) {
                _ in parses += 1
            }
        XCTAssertEqual(parses, 1)
        XCTAssertEqual(second.projects.first?.currentRound, 1)
    }
}
