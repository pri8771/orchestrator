import XCTest
@testable import OrchestratorGUI

@MainActor
final class BrainstormCommandTests: XCTestCase {
    func testMintBrainstormUsesRealNestedChatLifecycle() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("brainstorm-command-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = OrchestratorStore()
        store.rootURL = root

        let session = try XCTUnwrap(store.mintBrainstorm(project: "demo"))
        XCTAssertEqual(session.id, "demo/ideas/brainstorm")
        XCTAssertEqual(session.workflow, "chat_ideas")
        let dir = root.appendingPathComponent(session.id)
        XCTAssertEqual(try String(contentsOf: dir.appendingPathComponent("workflow.txt"),
                                  encoding: .utf8), "chat_ideas\n")
        XCTAssertEqual(try String(contentsOf: dir.appendingPathComponent(
            "initial_prompt/initial_prompt.md"), encoding: .utf8), "Let's brainstorm.")
    }
}
