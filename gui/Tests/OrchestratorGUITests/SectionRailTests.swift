import XCTest
@testable import OrchestratorGUI

// V3 board 3.8 (sub-PR 1): the section rail's explicit states and the
// truthfulness of its live status lines (R2/R4).
final class SectionRailTests: XCTestCase {

    private func tmp() -> URL {
        let u = FileManager.default.temporaryDirectory
            .appendingPathComponent("rail-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(
            at: u, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: u) }
        return u
    }

    private func mint(_ root: URL, _ name: String, manifest: String) {
        let d = root.appendingPathComponent(name)
        try? FileManager.default.createDirectory(
            at: d, withIntermediateDirectories: true)
        FileManager.default.createFile(
            atPath: d.appendingPathComponent("section.json").path,
            contents: Data(manifest.utf8))
    }

    func testDiscoveryStates() {
        let missing = tmp().appendingPathComponent("nope")
        XCTAssertEqual(SectionRailLogic.discover(sectionsDirURL: missing),
                       .empty)
        let empty = tmp()
        XCTAssertEqual(SectionRailLogic.discover(sectionsDirURL: empty),
                       .empty)
        let root = tmp()
        mint(root, "ideas", manifest: #"{"id": "ideas", "title": "Ideas"}"#)
        mint(root, "_template", manifest: "not even json")   // skipped
        mint(root, ".hidden", manifest: "{}")                // skipped
        guard case .populated(let metas) =
            SectionRailLogic.discover(sectionsDirURL: root) else {
            return XCTFail("expected populated")
        }
        XCTAssertEqual(metas.map(\.id), ["ideas"])
        XCTAssertEqual(metas[0].title, "Ideas")
    }

    func testCorruptManifestIsAnErrorNotSilence(){
        let root = tmp()
        mint(root, "broken", manifest: "{nope")
        guard case .error(let message) =
            SectionRailLogic.discover(sectionsDirURL: root) else {
            return XCTFail("a corrupt manifest must surface, not vanish")
        }
        XCTAssertTrue(message.contains("broken/section.json"))
    }

    private func proj(_ name: String, running: Bool = false,
                      phase: String? = nil, round: Int = 0,
                      awaiting: String? = nil) -> Project {
        var p = Project(name: name,
                        status: running ? .inProgress : .new,
                        currentPhase: phase, currentRound: round,
                        nextAgent: nil, error: nil, lastProcessed: nil,
                        completedPhases: [], phaseOutputs: [:],
                        dirURL: URL(fileURLWithPath: "/tmp/\(name)"))
        p.running = running
        p.awaitingHuman = awaiting
        return p
    }

    func testBelongsMatchesNestedAndLegacyIds() {
        XCTAssertTrue(SectionRailLogic.belongs("gloam/ideas/chat-1",
                                               toSection: "ideas"))
        XCTAssertTrue(SectionRailLogic.belongs("gloam--ideas--chat-1",
                                               toSection: "ideas"))
        XCTAssertFalse(SectionRailLogic.belongs("gloam/research/chat-1",
                                                toSection: "ideas"))
        XCTAssertFalse(SectionRailLogic.belongs("flatapp",
                                                toSection: "ideas"))
    }

    func testStatusLineIsTruthful() {
        // No chats — never a stale "running".
        XCTAssertEqual(SectionRailLogic.statusLine(section: "ideas",
                                                   projects: [proj("flat")]),
                       "no chats yet")
        // Idle chats show idle.
        XCTAssertEqual(SectionRailLogic.statusLine(
            section: "ideas", projects: [proj("g/ideas/one")]),
            "1 chat, idle")
        // A live debate names the chat, phase, and round.
        let live = proj("g/ideas/one", running: true,
                        phase: "initial_discussion", round: 3)
        let line = SectionRailLogic.statusLine(section: "ideas",
                                               projects: [live])
        XCTAssertTrue(line.hasPrefix("one — "), line)
        XCTAssertTrue(line.contains("round 3"), line)
        // Waiting-for-you outranks running (the human's move).
        let waiting = proj("g/ideas/two", awaiting: "chat")
        XCTAssertEqual(SectionRailLogic.statusLine(
            section: "ideas", projects: [live, waiting]),
            "two — waiting for you")
    }
}

// V3 board 3.8 (sub-PR 2): lint parsing + rules round-trip logic.
final class SectionSettingsLogicTests: XCTestCase {

    func testLintParserParsesTheFrozenContract() {
        let payload = """
        {"section": "ideas", "report": [
          {"severity": "error", "file": "rules.json", "field": "phases",
           "message": "must be an object"},
          {"severity": "warning", "file": "routing.json", "field": "-",
           "message": "does not parse"}]}
        """
        let summary = SectionLintParser.parse(Data(payload.utf8))
        XCTAssertEqual(summary?.errors, 1)
        XCTAssertEqual(summary?.warnings, 1)
        XCTAssertEqual(summary?.entries.first?.field, "phases")
    }

    func testLintParserFailureIsNilNeverClean() {
        XCTAssertNil(SectionLintParser.parse(Data("not json".utf8)))
        XCTAssertNil(SectionLintParser.parse(Data("{}".utf8)))
        let clean = SectionLintParser.parse(
            Data(#"{"section": "x", "report": []}"#.utf8))
        XCTAssertEqual(clean?.entries.count, 0, "empty report IS clean")
    }

    private func tmpRules() -> URL {
        let d = FileManager.default.temporaryDirectory
            .appendingPathComponent("rules-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: d) }
        return d.appendingPathComponent("rules.json")
    }

    func testRulesRoundTripPreservesOtherFields() {
        let url = tmpRules()
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        let original = """
        {"schema_version": 1, "phases": {"gather": {
          "rules": ["old rule"],
          "required_output": ["keep me"],
          "acceptance_checks": ["me too"]}}}
        """
        try? Data(original.utf8).write(to: url)
        var edited = SectionRulesLogic.load(rulesURL: url)!
        XCTAssertEqual(edited["gather"], "old rule")
        edited["gather"] = "new rule one\nnew rule two"
        XCTAssertTrue(SectionRulesLogic.save(edited: edited, rulesURL: url))
        // Re-read: rules changed, sibling fields survived.
        let reloaded = try! JSONSerialization.jsonObject(
            with: Data(contentsOf: url)) as! [String: Any]
        let gather = (reloaded["phases"] as! [String: Any])["gather"]
            as! [String: Any]
        XCTAssertEqual(gather["rules"] as? [String],
                       ["new rule one", "new rule two"])
        XCTAssertEqual(gather["required_output"] as? [String], ["keep me"])
        XCTAssertEqual(gather["acceptance_checks"] as? [String], ["me too"])
    }

    func testCorruptRulesFileLoadsAsNilNotEmpty() {
        let url = tmpRules()
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        try? Data("{broken".utf8).write(to: url)
        XCTAssertNil(SectionRulesLogic.load(rulesURL: url),
                     "corrupt must surface, not read as no-rules")
        XCTAssertEqual(SectionRulesLogic.load(
            rulesURL: url.deletingLastPathComponent()
                .appendingPathComponent("absent.json")), [:],
            "absent is genuinely empty")
    }

    func testSaveToUnwritableLocationReturnsFalse() {
        let url = URL(fileURLWithPath: "/nonexistent-root-dir/rules.json")
        XCTAssertFalse(SectionRulesLogic.save(edited: ["p": "r"],
                                              rulesURL: url),
                       "a failed write must never report Saved (R2)")
    }

    // A-60 regression: rules.json is engine-seeded, not GUI-owned — the
    // shipped template carries a top-level "_comment", and rebuilding the
    // root as {schema_version, phases} destroyed it (and downgraded any
    // future schema_version) on the first Save Rules.
    func testSaveKeepsUnknownTopLevelKeysAndSchemaVersion() {
        let url = tmpRules()
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        let original = """
        {"schema_version": 2, "_comment": "seeded note — hands off",
         "global_app_rules": ["fleet-wide rule"],
         "phases": {"gather": {"rules": ["old"]}}}
        """
        try? Data(original.utf8).write(to: url)
        XCTAssertTrue(SectionRulesLogic.save(edited: ["gather": "new"],
                                             rulesURL: url))
        let reloaded = try! JSONSerialization.jsonObject(
            with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertEqual(reloaded["_comment"] as? String, "seeded note — hands off")
        XCTAssertEqual(reloaded["global_app_rules"] as? [String],
                       ["fleet-wide rule"])
        XCTAssertEqual(reloaded["schema_version"] as? Int, 2,
                       "schema_version must never be downgraded")
        let gather = (reloaded["phases"] as! [String: Any])["gather"]
            as! [String: Any]
        XCTAssertEqual(gather["rules"] as? [String], ["new"])

        // A brand-new file still gets schema_version 1.
        let fresh = url.deletingLastPathComponent()
            .appendingPathComponent("fresh.json")
        XCTAssertTrue(SectionRulesLogic.save(edited: ["p": "r"],
                                             rulesURL: fresh))
        let obj = try! JSONSerialization.jsonObject(
            with: Data(contentsOf: fresh)) as! [String: Any]
        XCTAssertEqual(obj["schema_version"] as? Int, 1)
    }
}
