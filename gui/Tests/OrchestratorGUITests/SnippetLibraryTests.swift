import XCTest
@testable import OrchestratorGUI

final class SnippetLibraryTests: XCTestCase {
    private var temp: URL!

    override func setUpWithError() throws {
        temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("snippet-library-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: temp,
                                                withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: temp)
    }

    private func url(_ name: String) -> URL {
        temp.appendingPathComponent(name).appendingPathComponent("snippets.json")
    }

    private func snippet(_ name: String, _ text: String) -> PromptSnippet {
        PromptSnippet(name: name, phase: "", text: text)
    }

    // A-61 regression: save used to swallow write errors, so snippet edits
    // looked saved and silently vanished on relaunch. It must report whether
    // the write landed so saveSnippets can surface a failure.
    func testSaveReportsWhetherTheWriteLanded() throws {
        XCTAssertTrue(SnippetLibrary.save([snippet("a", "text")],
                                          to: url("fleet")))
        XCTAssertEqual(SnippetLibrary.loadLayer(at: url("fleet"),
                                                label: "fleet").snippets.count, 1)

        // A read-only parent dir: createDirectory succeeds (it exists), the
        // atomic write cannot land — save must say so, never pretend.
        let lockedDir = temp.appendingPathComponent("locked")
        try FileManager.default.createDirectory(at: lockedDir,
                                                withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o555],
                                              ofItemAtPath: lockedDir.path)
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o755], ofItemAtPath: lockedDir.path)
        }
        XCTAssertFalse(SnippetLibrary.save(
            [snippet("a", "text")],
            to: lockedDir.appendingPathComponent("snippets.json")),
            "a failed write must be reported, not swallowed")
    }

    func testProjectShadowsSectionShadowsFleetAndMissingUnshadows() throws {
        SnippetLibrary.save([snippet("simplify", "fleet"), snippet("fleet", "f")],
                            to: url("fleet"))
        SnippetLibrary.save([snippet("simplify", "section")], to: url("section"))
        SnippetLibrary.save([snippet("simplify", "project")], to: url("project"))
        var result = SnippetLibrary.load(fleetURL: url("fleet"),
                                         sectionURL: url("section"),
                                         projectURL: url("project"))
        XCTAssertEqual(result.snippets.first { $0.name == "simplify" }?.text,
                       "project")
        try FileManager.default.removeItem(at: url("project"))
        result = SnippetLibrary.load(fleetURL: url("fleet"),
                                     sectionURL: url("section"),
                                     projectURL: url("project"))
        XCTAssertEqual(result.snippets.first { $0.name == "simplify" }?.text,
                       "section")
        try FileManager.default.removeItem(at: url("section"))
        result = SnippetLibrary.load(fleetURL: url("fleet"),
                                     sectionURL: url("section"),
                                     projectURL: url("project"))
        XCTAssertEqual(result.snippets.first { $0.name == "simplify" }?.text,
                       "fleet")
        XCTAssertEqual(result.snippets.count, 2)
    }

    func testEightSeedsAppearOnlyWhenAbsentAndShippedFileMatches() throws {
        let fleet = url("fleet")
        SnippetLibrary.ensureSeeded(at: fleet)
        XCTAssertEqual(SnippetLibrary.loadLayer(at: fleet, label: "fleet").snippets.count,
                       8)
        let custom = Data("[{\"name\":\"mine\",\"phase\":\"\",\"text\":\"keep\"}]".utf8)
        try custom.write(to: fleet, options: .atomic)
        SnippetLibrary.ensureSeeded(at: fleet)
        XCTAssertEqual(try Data(contentsOf: fleet), custom)

        let shipped = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("library/snippets.json")
        let disk = SnippetLibrary.loadLayer(at: shipped, label: "shipped")
        XCTAssertEqual(disk.snippets, SnippetLibrary.seeded)
    }

    func testOldSchemaAndTypedVariablesRoundTripWithoutFieldLoss() {
        let variable = SnippetVariable(name: "tone", label: "Tone", kind: .choice,
                                      options: ["brief", "detailed"], required: true,
                                      defaultValue: "brief")
        let original = [snippet("old", "plain"),
                        PromptSnippet(name: "new", phase: "chat",
                                      text: "Make it {{tone}}", variables: [variable])]
        SnippetLibrary.save(original, to: url("fleet"))
        let loaded = SnippetLibrary.loadLayer(at: url("fleet"), label: "fleet")
        XCTAssertEqual(loaded.snippets, original)
        SnippetLibrary.save(loaded.snippets, to: url("roundtrip"))
        XCTAssertEqual(SnippetLibrary.loadLayer(at: url("roundtrip"), label: "again").snippets,
                       original)
    }

    func testCorruptLayerWarnsWithoutBlankingOthersAndMalformedVariablesDegrade() throws {
        SnippetLibrary.save([snippet("fleet", "safe")], to: url("fleet"))
        try FileManager.default.createDirectory(
            at: url("section").deletingLastPathComponent(),
            withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: url("project").deletingLastPathComponent(),
            withIntermediateDirectories: true)
        try Data("{bad".utf8).write(to: url("section"), options: .atomic)
        let malformed = """
        [{"name":"broken","phase":"","text":"Keep {{x}}","variables":{"name":"x"}}]
        """
        try Data(malformed.utf8).write(to: url("project"), options: .atomic)
        let result = SnippetLibrary.load(fleetURL: url("fleet"),
                                         sectionURL: url("section"),
                                         projectURL: url("project"))
        XCTAssertEqual(Set(result.snippets.map(\.name)), ["fleet", "broken"])
        XCTAssertTrue(result.warnings.contains { $0.contains("section") })
        let broken = try XCTUnwrap(result.snippets.first { $0.name == "broken" })
        XCTAssertTrue(broken.variables.isEmpty)
        XCTAssertNotNil(broken.warning)
        XCTAssertEqual(broken.text, "Keep {{x}}")
    }

    func testTypedFormGatesRequiredChoiceAndRendersWithoutInventingTokens() {
        let variables = [
            SnippetVariable(name: "count", label: "Count", kind: .number,
                            options: [], required: true, defaultValue: "2"),
            SnippetVariable(name: "tone", label: "Tone", kind: .choice,
                            options: ["brief", "deep"], required: true,
                            defaultValue: nil),
            SnippetVariable(name: "flag", label: "Flag", kind: .boolean,
                            options: [], required: false, defaultValue: "false"),
        ]
        let snippet = PromptSnippet(name: "typed", phase: "",
                                    text: "{{count}} {{tone}} {{flag}} {{unknown}}",
                                    variables: variables)
        var form = SnippetFormDraft(snippet: snippet)
        XCTAssertFalse(form.canInsert)
        XCTAssertEqual(form.blockingVariables.map(\.name), ["tone"])
        form.values["tone"] = "brief"
        XCTAssertTrue(form.canInsert)
        XCTAssertEqual(form.renderedText, "2 brief false {{unknown}}")
        XCTAssertEqual(form.lintWarnings,
                       ["Undeclared token {{unknown}} remains literal."])
        form.values["count"] = "not-number"
        XCTAssertFalse(form.canInsert)
    }

    func testAutocompleteAndSnippetCommandInsertPathNeverImplicitlySend() {
        let snippets = [snippet("simplify", "editable text"),
                        snippet("ship-check", "ship")]
        XCTAssertEqual(SnippetComposerLogic.matches(draft: "/sim", snippets: snippets)
            .map(\.name), ["simplify"])
        XCTAssertEqual(SnippetComposerLogic.matches(draft: "/snippet ship",
                                                    snippets: snippets).map(\.name),
                       ["ship-check"])
        XCTAssertEqual(SnippetComposerLogic.resolveCommand(
            "/snippet simplify", snippets: snippets), .snippet(snippets[0]))
        guard case .refusal(let message) = SnippetComposerLogic.resolveCommand(
            "/snippet missing", snippets: snippets) else {
            return XCTFail("unknown snippet must refuse")
        }
        XCTAssertIn(message, "Unknown snippet")
        XCTAssertIn(message, "nothing was sent")
        XCTAssertEqual(SnippetComposerLogic.resolveCommand(
            "ordinary message", snippets: snippets), .notCommand)
    }
}

private extension XCTestCase {
    func XCTAssertIn(_ value: String, _ substring: String,
                     file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(value.contains(substring), "\(value) lacks \(substring)",
                      file: file, line: line)
    }
}
