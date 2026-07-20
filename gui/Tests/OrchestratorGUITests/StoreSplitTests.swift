import XCTest
@testable import OrchestratorGUI

@MainActor
final class StoreSplitTests: XCTestCase {
    func testDeadOrRecycledPidIsNeverSignaledAndItsLockIsCleared() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("run-controller-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let lock = dir.appendingPathComponent("session.lock")
        try Data("pid=424242\n".utf8).write(to: lock)
        var signals: [(Int32, Int32)] = []
        var refreshes = 0
        let controller = RunController(
            isPidAlive: { _ in false },
            signalPid: { signals.append(($0, $1)) },
            graceNanoseconds: 0)
        let hooks = RunController.Hooks(
            appendLog: { _ in },
            surfaceError: { XCTFail($0) },
            refresh: { refreshes += 1 },
            terminated: { _, _, _ in })

        controller.stopExternal("session", pid: 424242,
                                lockURL: lock, hooks: hooks)

        XCTAssertTrue(signals.isEmpty,
                      "a failed liveness probe must block SIGTERM and SIGKILL")
        XCTAssertFalse(FileManager.default.fileExists(atPath: lock.path))
        XCTAssertEqual(refreshes, 1)
    }

    func testSessionModelOwnsIndependentTailAndChatState() {
        let first = SessionModel(id: "project/ideas/one")
        let second = SessionModel(id: "project/ideas/two")
        first.chatInput = "draft one"
        first.chatThinking = true
        first.streamTailCache = SessionModel.StreamTailCache(
            path: "/tmp/one", turnID: "chat:r1:api:turn", agent: "api:test",
            offset: 4, remainder: Data(), text: "delta", mtime: Date(), lastSeq: 1)

        XCTAssertEqual(first.chatInput, "draft one")
        XCTAssertTrue(first.chatThinking)
        XCTAssertEqual(first.streamTailCache?.text, "delta")
        XCTAssertEqual(second.chatInput, "")
        XCTAssertFalse(second.chatThinking)
        XCTAssertNil(second.streamTailCache)
    }
}
