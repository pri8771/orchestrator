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
