import XCTest
@testable import OrchestratorGUI

// V3 board 1.4: per-chat history persistence. Exercises ChatHistoryStore
// against a temp directory ONLY — never OrchestratorStore, whose URLs point
// at the developer's real Application Support and workspace.
final class ChatHistoryTests: XCTestCase {

    private var tmp: URL!
    private var store: ChatHistoryStore!

    override func setUp() {
        super.setUp()
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("chat-history-tests-\(UUID().uuidString)")
        store = ChatHistoryStore(baseDir: tmp.appendingPathComponent("chat_history"))
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tmp)
        super.tearDown()
    }

    private func msg(_ text: String, user: Bool = true) -> ConciergeMessage {
        ConciergeMessage(role: user ? .user : .concierge, text: text)
    }

    func testKeyToURLMappingIsSlugifiedAndNamespaced() {
        let url = ChatHistoryStore.fileURL(for: "home", baseDir: tmp)
        XCTAssertEqual(url.lastPathComponent, "chat-home.json")
        // A hostile key cannot escape the directory or collide unpredictably.
        let weird = ChatHistoryStore.fileURL(for: "../Weird Key!", baseDir: tmp)
        XCTAssertEqual(weird.deletingLastPathComponent().path, tmp.path)
        XCTAssertEqual(weird.lastPathComponent, "chat-weird-key.json")
    }

    func testSaveLoadRoundTrip() throws {
        try store.save([msg("hello"), msg("reply", user: false)], key: "home")
        let loaded = store.load(key: "home")
        XCTAssertEqual(loaded?.count, 2)
        XCTAssertEqual(loaded?.first?.text, "hello")
    }

    func testTwoKeysAreIsolated() throws {
        try store.save([msg("alpha only")], key: "alpha")
        try store.save([msg("beta only")], key: "beta")
        XCTAssertEqual(store.load(key: "alpha")?.map(\.text), ["alpha only"])
        XCTAssertEqual(store.load(key: "beta")?.map(\.text), ["beta only"])
    }

    func testMissingKeyLoadsNilNotPreviousChat() {
        XCTAssertNil(store.load(key: "never-written"),
                     "missing file must read as nil so the caller resets to []")
    }

    func testLateReplyAppendsToOriginKeyFile() throws {
        try store.save([msg("question")], key: "origin")
        try store.append(msg("late reply", user: false), key: "origin")
        XCTAssertEqual(store.load(key: "origin")?.map(\.text),
                       ["question", "late reply"])
        // The append never touched any other key.
        XCTAssertNil(store.load(key: "home"))
    }

    func testAppendToUnwrittenKeyCreatesIt() throws {
        try store.append(msg("first", user: false), key: "fresh")
        XCTAssertEqual(store.load(key: "fresh")?.map(\.text), ["first"])
    }

    func testLegacyMigrationCopiesToHomeOnceAndKeepsOriginal() throws {
        let legacy = tmp.appendingPathComponent("chat_history.json")
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        try JSONEncoder().encode([msg("old conversation")]).write(to: legacy)

        store.migrateLegacyIfNeeded(legacyURL: legacy, homeKey: "home")
        XCTAssertEqual(store.load(key: "home")?.map(\.text), ["old conversation"],
                       "legacy history must appear as the Home chat's history")
        XCTAssertTrue(FileManager.default.fileExists(atPath: legacy.path),
                      "legacy file is left in place")

        // Second migration is a no-op: post-upgrade history is never clobbered.
        try store.save([msg("new era")], key: "home")
        store.migrateLegacyIfNeeded(legacyURL: legacy, homeKey: "home")
        XCTAssertEqual(store.load(key: "home")?.map(\.text), ["new era"])
    }

    func testMigrationWithGarbageLegacyFileIsSafeNoop() throws {
        let legacy = tmp.appendingPathComponent("chat_history.json")
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        try Data("not json at all".utf8).write(to: legacy)
        store.migrateLegacyIfNeeded(legacyURL: legacy, homeKey: "home")
        XCTAssertNil(store.load(key: "home"))
    }
}
