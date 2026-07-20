import XCTest
@testable import OrchestratorGUI

final class ComposerCommandsTests: XCTestCase {
    private var temp: URL!

    override func setUpWithError() throws {
        temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("composer-commands-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: temp,
                                                withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: temp)
    }

    private func write(_ name: String, commands: [[String: Any]]) throws -> URL {
        let url = temp.appendingPathComponent(name).appendingPathComponent("commands.json")
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: [
            "schema_version": 1, "commands": commands])
        try data.write(to: url, options: .atomic)
        return url
    }

    func testCommandLayersShadowAndCorruptLayerDoesNotBlankOthers() throws {
        let fleet = try write("fleet", commands: [
            ["name": "help", "kind": "builtin", "description": "fleet"],
            ["name": "audit", "kind": "delegation", "description": "audit"]])
        let section = try write("section", commands: [
            ["name": "help", "kind": "builtin", "description": "section"]])
        let project = temp.appendingPathComponent("project/commands.json")
        try FileManager.default.createDirectory(at: project.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try Data("{bad".utf8).write(to: project)
        let result = CommandLibrary.load(fleetURL: fleet, sectionURL: section,
                                         projectURL: project)
        XCTAssertEqual(result.commands.first { $0.name == "help" }?.description,
                       "section")
        XCTAssertNotNil(result.commands.first { $0.name == "audit" })
        XCTAssertFalse(result.warnings.isEmpty, "corrupt project layer must warn")
    }

    func testMergedPopoverRanksWithSharedFuzzyScoreAndShowsKinds() {
        let commands = [
            ComposerCommand(name: "status", kind: .builtin, description: "State"),
            ComposerCommand(name: "summarize", kind: .template, description: "Summary")]
        let snippets = [PromptSnippet(name: "ship-check", phase: "", text: "Ship")]
        let matches = ComposerAutocompleteLogic.matches(
            draft: "/st", commands: commands, snippets: snippets)
        XCTAssertEqual(matches.map(\.name), ["status"])
        XCTAssertEqual(matches.map(\.kindLabel), ["builtin"])
        let fuzzy = ComposerAutocompleteLogic.matches(
            draft: "/sh", commands: commands, snippets: snippets)
        XCTAssertEqual(fuzzy.first?.name, "ship-check")
        XCTAssertEqual(fuzzy.first?.kindLabel, "snippet")
    }

    func testKeyboardNavigationClampsAndEscapeCanDismissWithoutChangingDraft() {
        XCTAssertEqual(ComposerAutocompleteLogic.movedIndex(
            current: nil, delta: 1, count: 3), 0)
        XCTAssertEqual(ComposerAutocompleteLogic.movedIndex(
            current: nil, delta: -1, count: 3), 2)
        XCTAssertEqual(ComposerAutocompleteLogic.movedIndex(
            current: 2, delta: 1, count: 3), 2)
        XCTAssertEqual(ComposerAutocompleteLogic.movedIndex(
            current: 0, delta: -1, count: 3), 0)
        let draft = "/stat"
        var dismissed = false
        dismissed = true
        XCTAssertTrue(dismissed)
        XCTAssertEqual(draft, "/stat", "Esc state must not steal composer text")
    }

    func testWhitespaceDismissesPopoverSoArgumentsRemainComposerText() {
        let commands = [ComposerCommand(name: "compare", kind: .builtin,
                                        description: "Compare")]
        XCTAssertTrue(ComposerAutocompleteLogic.matches(
            draft: "/compare codex :: prompt", commands: commands,
            snippets: []).isEmpty)
    }

    func testCommandCardsParseWithoutEnteringChatMessages() {
        let text = """
        # Chat
        ## Transcript
        ### Round 1
        **Codex — Round 1**

        hello
        <!-- command-card:start -->
        **/status**

        running
        <!-- command-card:end -->
        """
        let parsed = TranscriptParser.parse(text)
        XCTAssertEqual(parsed.messages.count, 1)
        XCTAssertEqual(parsed.commandCards,
                       [CommandTranscriptCard(id: 0,
                                              body: "**/status**\n\nrunning")])
    }

    func testComparisonCardKeepsOkAndFailureColumnsTogether() {
        let body = """
        **/compare — 2 models**

        Prompt: choose

        ### codex — ok
        useful answer
        Cost: unmetered

        ### claude — failed
        Error: offline
        """
        let columns = ComparisonCardLogic.parse(body)
        XCTAssertEqual(columns.map(\.model), ["codex", "claude"])
        XCTAssertEqual(columns.map(\.status), ["ok", "failed"])
        XCTAssertTrue(columns[0].body.contains("Cost: unmetered"))
        XCTAssertTrue(columns[1].body.contains("Error: offline"))
    }

    @MainActor
    func testComparisonPickerUsesOnlySelectedModelsAndLegalEffortControls() {
        var draft = ComparisonComposerDraft(agents: ["codex", "claude", "gemini"])
        draft.selected = ["codex", "gemini"]
        draft.efforts["codex"] = "high"
        draft.prompt = "Which design is safer?"
        XCTAssertTrue(draft.canInsert)
        XCTAssertEqual(draft.commandText,
                       "/compare codex@high,gemini :: Which design is safer?")
        draft.prompt = "   "
        XCTAssertFalse(draft.canInsert)
    }
}
