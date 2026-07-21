import XCTest
@testable import OrchestratorGUI

final class ProjectArchiveTests: XCTestCase {
    func testNestedSessionArchiveTargetsWholeProjectAndExplainsMove() {
        XCTAssertEqual(ProjectArchivePresentation.projectSlug(
            for: "atlas/ideas/chat-1"), "atlas")
        let text = ProjectArchivePresentation.confirmation(
            project: "atlas", stopping: true)
        XCTAssertTrue(text.contains("stopped first"))
        XCTAssertTrue(text.contains("workspace/.archive/atlas"))
        XCTAssertTrue(text.contains("disappears from engine/search/GUI discovery"))
        XCTAssertTrue(text.contains("--unarchive-project"))
        XCTAssertTrue(text.contains("Nothing is deleted"))
    }

    func testArchiveDirectoryIsAbsentFromGUIDiscovery() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(
            "archive-discovery-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let active = root.appendingPathComponent("active/initial_prompt",
                                                 isDirectory: true)
        let archived = root.appendingPathComponent(
            ".archive/hidden/initial_prompt", isDirectory: true)
        try FileManager.default.createDirectory(at: active,
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: archived,
                                                withIntermediateDirectories: true)
        try Data("a".utf8).write(to: active.appendingPathComponent(
            "initial_prompt.md"))
        try Data("h".utf8).write(to: archived.appendingPathComponent(
            "initial_prompt.md"))
        XCTAssertEqual(SessionLayout.discoverApps(rootURL: root), ["active"])
    }
}
