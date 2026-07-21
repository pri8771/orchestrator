import XCTest
@testable import OrchestratorGUI

final class PrivacyTieringTests: XCTestCase {
    private func tempDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("privacy-gui-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url,
                                                withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testSensitivityWritePreservesRunConfigAndSupportsExplicitOff() throws {
        let dir = try tempDir()
        let url = dir.appendingPathComponent("run_config.json")
        try Data("{\"autonomy\":\"manual\",\"api_agents\":true}".utf8)
            .write(to: url)
        XCTAssertTrue(ProjectSensitivityFile.write("private", to: dir))
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: url))
                as? [String: Any])
        XCTAssertEqual(object["autonomy"] as? String, "manual")
        XCTAssertEqual(object["api_agents"] as? Bool, true)
        XCTAssertEqual(object["sensitivity"] as? String, "private")
        XCTAssertTrue(ProjectSensitivityFile.write("normal", to: dir))
        XCTAssertEqual(ProjectSensitivityFile.read(dir), "normal")
    }

    func testProjectPrivateIsFloorAndCorruptConfigIsNeverOverwritten() throws {
        let project = try tempDir()
        let session = project.appendingPathComponent("ideas/chat",
                                                     isDirectory: true)
        try FileManager.default.createDirectory(at: session,
                                                withIntermediateDirectories: true)
        XCTAssertTrue(ProjectSensitivityFile.write("private", to: project))
        XCTAssertTrue(ProjectSensitivityFile.write("normal", to: session))
        XCTAssertEqual(ProjectSensitivityFile.effective(
            projectDir: project, sessionDir: session), "private")
        let corrupt = try tempDir()
        let path = corrupt.appendingPathComponent("run_config.json")
        try Data("not-json".utf8).write(to: path)
        XCTAssertFalse(ProjectSensitivityFile.write("private", to: corrupt))
        XCTAssertEqual(try String(contentsOf: path, encoding: .utf8), "not-json")
    }

    func testConflictIsImmediateUntilLocalExecutionIsReal() {
        XCTAssertNotNil(ProjectSensitivityFile.conflict(
            localEnabled: false, installedLocalCount: 2))
        XCTAssertNotNil(ProjectSensitivityFile.conflict(
            localEnabled: true, installedLocalCount: 0))
        XCTAssertNil(ProjectSensitivityFile.conflict(
            localEnabled: true, installedLocalCount: 1))
    }

    func testBackgroundScanReflectsPersistedPrivateState() throws {
        let root = try tempDir()
        let dir = root.appendingPathComponent("demo", isDirectory: true)
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        XCTAssertTrue(ProjectSensitivityFile.write("private", to: dir))
        let wf = WorkflowDef(name: "chat", title: "Chat", description: "",
                             target: "answer", phases: [])
        let projects = BackgroundProjectLoader.loadProjects(
            names: ["demo"], rootURL: root,
            workflowsByName: ["chat": wf], defaultWorkflow: wf,
            manualStops: [:], runningProcessNames: [])
        XCTAssertEqual(projects.count, 1)
        XCTAssertTrue(projects[0].isPrivate)
    }
}
