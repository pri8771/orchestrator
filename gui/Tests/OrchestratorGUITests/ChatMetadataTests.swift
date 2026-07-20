import XCTest
@testable import OrchestratorGUI

final class ChatMetadataTests: XCTestCase {
    private func tempTranscript(_ body: Data = Data()) throws -> (root: URL, url: URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("chat-meta-\(UUID().uuidString)")
        let url = root.appendingPathComponent("demo/ideas/chat/chat/chat.md")
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try body.write(to: url)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return (root, url)
    }

    func testRoundTripChangesOnlyTheSingleMetaLine() throws {
        let body = Data("# Demo\r\n\r\n## Transcript\r\nbody\r\n".utf8)
        let item = try tempTranscript(body)
        let first = ChatMeta(pinned: true, tags: ["pricing", "launch"])
        try ChatMetaDocument.write(first, to: item.url)
        XCTAssertEqual(ChatMetaDocument.read(item.url),
                       ChatMetaRead(meta: first, warning: nil))
        XCTAssertEqual(ChatMetaDocument.bodyBytes(from: try Data(contentsOf: item.url)), body)

        let second = ChatMeta(pinned: false, tags: ["edited"])
        try ChatMetaDocument.write(second, to: item.url)
        XCTAssertEqual(ChatMetaDocument.read(item.url).meta, second)
        XCTAssertEqual(ChatMetaDocument.bodyBytes(from: try Data(contentsOf: item.url)), body)
    }

    func testMalformedLineDegradesLoudlyAndCanBeRepairedWithoutBodyLoss() throws {
        let body = Data("## Transcript\n**You — Round 1**\nhello\n".utf8)
        let raw = Data("<!-- chat-meta: {bad} -->\n".utf8) + body
        let item = try tempTranscript(raw)
        let parsed = ChatMetaDocument.read(item.url)
        XCTAssertEqual(parsed.meta, ChatMeta())
        XCTAssertNotNil(parsed.warning)

        let repaired = ChatMeta(pinned: true, tags: [])
        try ChatMetaDocument.write(repaired, to: item.url)
        XCTAssertEqual(ChatMetaDocument.read(item.url).meta, repaired)
        XCTAssertEqual(ChatMetaDocument.bodyBytes(from: try Data(contentsOf: item.url)), body)
    }

    func testAppendRacingMetaUpsertLosesNeitherSide() throws {
        let original = Data("## Transcript\nfirst\n".utf8)
        let appended = Data("**Codex — Round 2**\nsecond\n".utf8)
        let item = try tempTranscript(original)
        try ChatMetaDocument.write(ChatMeta(pinned: true, tags: ["race"]), to: item.url) {
            let handle = try? FileHandle(forWritingTo: item.url)
            _ = try? handle?.seekToEnd()
            try? handle?.write(contentsOf: appended)
            try? handle?.close()
        }
        XCTAssertEqual(ChatMetaDocument.read(item.url).meta,
                       ChatMeta(pinned: true, tags: ["race"]))
        XCTAssertEqual(ChatMetaDocument.bodyBytes(from: try Data(contentsOf: item.url)),
                       original + appended)
    }

    func testIndexKeepsArchivedChatsVisibleOnlyInRestoreCollection() throws {
        let item = try tempTranscript(Data("## Transcript\n".utf8))
        let project = item.root.appendingPathComponent("demo")
        try Data().write(to: project.appendingPathComponent(".orch-sections"))
        let session = project.appendingPathComponent("ideas/chat")
        try FileManager.default.createDirectory(
            at: session.appendingPathComponent("initial_prompt"),
            withIntermediateDirectories: true)
        try Data().write(to: session.appendingPathComponent(
            "initial_prompt/initial_prompt.md"))
        try Data().write(to: session.appendingPathComponent(".orch_archived"))

        let snapshot = ChatMetadataIndex.scan(rootURL: item.root)
        XCTAssertEqual(snapshot.archived.map(\.id), ["demo/ideas/chat"])
        XCTAssertEqual(snapshot.metadata["demo/ideas/chat"], ChatMeta())
        XCTAssertTrue(snapshot.transcriptAvailable.contains("demo/ideas/chat"))
        try FileManager.default.removeItem(at: session.appendingPathComponent(".orch_archived"))
        XCTAssertTrue(ChatMetadataIndex.scan(rootURL: item.root).archived.isEmpty)
    }

    func testPinnedFirstTagFilterAndEditorNormalization() {
        let meta = [
            "p/ideas/zulu": ChatMeta(pinned: true, tags: ["pricing"]),
            "p/ideas/alpha": ChatMeta(pinned: false, tags: ["launch"]),
            "p/ideas/beta": ChatMeta(pinned: false, tags: ["Pricing"])
        ]
        let ids = ["p/ideas/alpha", "p/ideas/beta", "p/ideas/zulu"]
        XCTAssertEqual(ChatSidebarLogic.visibleIDs(ids, metadata: meta, tag: nil),
                       ["p/ideas/zulu", "p/ideas/alpha", "p/ideas/beta"])
        XCTAssertEqual(ChatSidebarLogic.visibleIDs(ids, metadata: meta, tag: "pricing"),
                       ["p/ideas/zulu", "p/ideas/beta"])
        XCTAssertEqual(ChatTagEditorLogic.parse(" Pricing, launch, pricing,  "),
                       ["Pricing", "launch"])
    }
}
