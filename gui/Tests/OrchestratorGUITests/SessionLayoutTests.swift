import XCTest
@testable import OrchestratorGUI

// V3 board 3.0 (sub-PR B): the Swift half of the nested-layout contract.
// The lock-encoding cases come from the SHARED fixture
// tests/fixtures/lock_encoding.json — the same file the Python suite
// pins. Neither implementation may "adjust" the fixture to pass.
final class SessionLayoutTests: XCTestCase {

    private func fixtureURL() -> URL {
        // #filePath -> …/gui/Tests/OrchestratorGUITests/SessionLayoutTests.swift
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // OrchestratorGUITests/
            .deletingLastPathComponent()   // Tests/
            .deletingLastPathComponent()   // gui/
            .deletingLastPathComponent()   // repo root
            .appendingPathComponent("tests/fixtures/lock_encoding.json")
    }

    func testFixtureParityWithTheEngine() throws {
        let data = try Data(contentsOf: fixtureURL())
        let doc = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let cases = doc["cases"] as! [[String: String]]
        XCTAssertGreaterThanOrEqual(cases.count, 10)
        for c in cases {
            XCTAssertEqual(SessionLayout.encodeLockName(c["id"]!), c["lock"]!,
                           "encoding diverged from the engine for \(c["id"]!)")
        }
    }

    func testDecodeRoundTripsAndFlatStemsPassThrough() throws {
        let data = try Data(contentsOf: fixtureURL())
        let doc = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        for c in doc["cases"] as! [[String: String]] {
            let id = c["id"]!, lock = c["lock"]!
            if id.contains("/") {
                // NFC/NFD spellings encode to ONE stem which decodes to
                // the NFC form — compare normalized.
                XCTAssertEqual(SessionLayout.decodeLockStem(lock),
                               id.precomposedStringWithCanonicalMapping)
            } else {
                XCTAssertEqual(SessionLayout.decodeLockStem(lock), id,
                               "flat stems must pass through raw")
            }
        }
        // A flat dir that merely LOOKS encoded fails the hash round-trip.
        XCTAssertEqual(SessionLayout.decodeLockStem("proj%2Fs%2Fc.00000000"),
                       "proj%2Fs%2Fc.00000000")
    }

    private func mkdir(_ url: URL) {
        try? FileManager.default.createDirectory(
            at: url, withIntermediateDirectories: true)
    }

    private func touch(_ url: URL, _ text: String = "") {
        mkdir(url.deletingLastPathComponent())
        FileManager.default.createFile(atPath: url.path,
                                       contents: Data(text.utf8))
    }

    func testDiscoveryMirrorsTheEngineRules() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("layout-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        // flat project
        touch(root.appendingPathComponent("flatapp/initial_prompt/initial_prompt.md"))
        // nested under a marked project
        touch(root.appendingPathComponent("gloam/.orch-sections"))
        touch(root.appendingPathComponent("gloam/ideas/first/initial_prompt/initial_prompt.md"))
        // unmarked wrapper: never discovered
        touch(root.appendingPathComponent("backups/keep/old/initial_prompt/initial_prompt.md"))
        // legacy project with a root agent_state.json: never recursed
        touch(root.appendingPathComponent("legacy/agent_state.json"), "{}")
        touch(root.appendingPathComponent("legacy/.orch-sections"))
        touch(root.appendingPathComponent("legacy/s/c/initial_prompt/initial_prompt.md"))
        // flat project is never recursed even with a marker
        touch(root.appendingPathComponent("flatapp/.orch-sections"))
        touch(root.appendingPathComponent("flatapp/s/c/initial_prompt/initial_prompt.md"))
        // archived nested chat invisible
        touch(root.appendingPathComponent("gloam/ideas/dead/initial_prompt/initial_prompt.md"))
        touch(root.appendingPathComponent("gloam/ideas/dead/.orch_archived"))
        // unaddressable segment skipped
        touch(root.appendingPathComponent("gloam/ideas/a..b/initial_prompt/initial_prompt.md"))
        XCTAssertEqual(SessionLayout.discoverApps(rootURL: root),
                       ["flatapp", "gloam/ideas/first"])
        try FileManager.default.removeItem(at: root.appendingPathComponent(
            "gloam/ideas/dead/.orch_archived"))
        XCTAssertEqual(SessionLayout.discoverApps(rootURL: root),
                       ["flatapp", "gloam/ideas/dead", "gloam/ideas/first"])
    }

    func testParentConflictMirrorsTheEngine() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("conflict-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        touch(root.appendingPathComponent("flat/initial_prompt/initial_prompt.md"))
        XCTAssertEqual(ChatSessionMint.parentConflict(rootURL: root,
                                                      project: "flat"),
                       "existing flat project")
        touch(root.appendingPathComponent("legacy/agent_state.json"), "{}")
        XCTAssertEqual(ChatSessionMint.parentConflict(rootURL: root,
                                                      project: "legacy"),
                       "existing legacy project")
        touch(root.appendingPathComponent("wrapper/somefile.txt"), "x")
        XCTAssertEqual(ChatSessionMint.parentConflict(rootURL: root,
                                                      project: "wrapper"),
                       "existing unmarked directory")
        XCTAssertNil(ChatSessionMint.parentConflict(rootURL: root,
                                                    project: "fresh"))
        mkdir(root.appendingPathComponent("empty"))
        XCTAssertNil(ChatSessionMint.parentConflict(rootURL: root,
                                                    project: "empty"))
    }

    func testMintCreatesNestedLayoutWithMarker() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("mint-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        mkdir(root)
        let minted = try ChatSessionMint.mintChatDir(
            rootURL: root, project: "Gloam", section: "Ideas",
            title: "First Chat", workflow: "chat_ideas", firstMessage: "hi")
        XCTAssertEqual(minted.name, "gloam/ideas/first-chat")
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: root.appendingPathComponent("gloam/.orch-sections").path))
        XCTAssertEqual(SessionLayout.discoverApps(rootURL: root),
                       ["gloam/ideas/first-chat"])
        // collision suffixes on the chat segment
        let second = try ChatSessionMint.mintChatDir(
            rootURL: root, project: "Gloam", section: "Ideas",
            title: "First Chat", workflow: "chat_ideas", firstMessage: "hi")
        XCTAssertEqual(second.name, "gloam/ideas/first-chat-2")
    }
}
