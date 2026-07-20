import XCTest
@testable import OrchestratorGUI

final class StreamingLifecycleTests: XCTestCase {
    private var tmp: URL!

    override func setUpWithError() throws {
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("orch-stream-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: tmp,
                                                withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: tmp)
    }

    func testPaneStateUsesDescriptorAndNeverFakesCLIStreaming() {
        let preview = StreamPreview(agent: "api:openai:gpt-5",
                                    turnID: "design:2:api:openai:gpt-5:turn",
                                    text: "live")
        XCTAssertEqual(PaneTurnState.resolve(isActive: true,
                                             agent: "api:openai:gpt-5",
                                             live: true, supportsStreams: true,
                                             preview: preview),
                       .streaming(preview))
        XCTAssertEqual(PaneTurnState.resolve(isActive: true, agent: "claude",
                                             live: true, supportsStreams: false,
                                             preview: StreamPreview(agent: "claude",
                                                                    turnID: "x",
                                                                    text: "fake")),
                       .waiting(agent: "claude", live: true))
        XCTAssertEqual(PaneTurnState.resolve(isActive: false,
                                             agent: "api:openai:gpt-5",
                                             live: false, supportsStreams: true,
                                             preview: preview), .final)
    }

    func testOnlyFocusedLiveCapablePaneMayTouchStreamDirectory() {
        XCTAssertTrue(OrchestratorStore.shouldReadStream(
            focusedPane: "p/s/chat", project: "p/s/chat", running: true,
            supportsStreams: true))
        XCTAssertFalse(OrchestratorStore.shouldReadStream(
            focusedPane: "other", project: "p/s/chat", running: true,
            supportsStreams: true))
        XCTAssertFalse(OrchestratorStore.shouldReadStream(
            focusedPane: "p/s/chat", project: "p/s/chat", running: false,
            supportsStreams: true))
        XCTAssertFalse(OrchestratorStore.shouldReadStream(
            focusedPane: "p/s/chat", project: "p/s/chat", running: true,
            supportsStreams: false))
    }

    func testTailIsIncrementalOrderedAndAgentScoped() throws {
        let dir = tmp.appendingPathComponent(".stream", isDirectory: true)
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        let api = "api:openai:gpt-5"
        let turnID = "design:2:\(api):turn"
        let url = dir.appendingPathComponent("\(turnID).ndjson")
        let other = dir.appendingPathComponent("design:2:claude:turn.ndjson")
        try #"{"seq":1,"ts":1,"delta":"hel"}"#.appending("\n")
            .write(to: url, atomically: true, encoding: .utf8)
        try #"{"seq":1,"ts":1,"delta":"wrong"}"#.appending("\n")
            .write(to: other, atomically: true, encoding: .utf8)

        let first = OrchestratorStore.readStreamTail(in: dir, agent: api,
                                                      prior: nil)
        XCTAssertEqual(first.1?.turnID, turnID)
        XCTAssertEqual(first.1?.text, "hel")
        XCTAssertNotNil(first.0)

        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        handle.write(Data(#"{"seq":2,"ts":2,"delta":"lo"}"#.appending("\n").utf8))
        try handle.close()
        let second = OrchestratorStore.readStreamTail(in: dir, agent: api,
                                                       prior: first.0)
        XCTAssertEqual(second.1?.text, "hello")
        XCTAssertEqual(second.0?.offset,
                       UInt64((try Data(contentsOf: url)).count))
    }

    func testSplitUnicodeAndDuplicateSequenceDoNotCorruptPreview() throws {
        let dir = tmp.appendingPathComponent(".stream", isDirectory: true)
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        let api = "api:openai:gpt-5"
        let url = dir.appendingPathComponent("design:1:\(api):turn.ndjson")
        // No final newline: first poll must retain the raw bytes (including a
        // multibyte scalar) without decoding/replacement or advancing seq.
        let firstBytes = Data(#"{"seq":1,"ts":1,"delta":"café"}"#.utf8)
        try firstBytes.write(to: url)
        let first = OrchestratorStore.readStreamTail(in: dir, agent: api,
                                                      prior: nil)
        XCTAssertEqual(first.1?.text, "")
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        handle.write(Data("\n".utf8))
        handle.write(Data(#"{"seq":1,"ts":2,"delta":" DUP"}"#.appending("\n").utf8))
        handle.write(Data(#"{"seq":2,"ts":3,"delta":" ✓"}"#.appending("\n").utf8))
        try handle.close()
        let final = OrchestratorStore.readStreamTail(in: dir, agent: api,
                                                      prior: first.0)
        XCTAssertEqual(final.1?.text, "café ✓")
    }

    func testTranscriptReadIgnoresLiveStreamSibling() throws {
        let phase = tmp.appendingPathComponent("phase.md")
        let authoritative = """
        # Phase

        ## Transcript

        ### Round 1

        **Claude — Round 1**

        authoritative
        """
        try authoritative.write(to: phase, atomically: true, encoding: .utf8)
        let streamDir = tmp.appendingPathComponent(".stream", isDirectory: true)
        try FileManager.default.createDirectory(at: streamDir,
                                                withIntermediateDirectories: true)
        try #"{"seq":1,"ts":1,"delta":"POISON PREVIEW"}"#
            .write(to: streamDir.appendingPathComponent(
                "design:1:api:openai:gpt-5:turn.ndjson"),
                   atomically: true, encoding: .utf8)

        let result = OrchestratorStore.readAndParseTranscript(
            at: phase, ifChangedSince: nil)
        XCTAssertNotNil(result.fresh)
        XCTAssertEqual(result.fresh?.messages.map(\.body), ["authoritative"])
        XCTAssertFalse(result.fresh?.messages.contains {
            $0.body.contains("POISON PREVIEW")
        } ?? true)
    }
}
