import XCTest
@testable import OrchestratorGUI

// V3 board 1.5: pure reducer matrix + mint statics — no OrchestratorStore
// instantiation (its URLs point at real user data).
final class ChatSessionLifecycleTests: XCTestCase {

    // MARK: termination reducer

    func testStopRequestAlwaysMapsToStopped() {
        for (code, signal) in [(Int32(0), false), (1, false), (9, true)] {
            let s = ChatSessionState.afterTermination(
                status: code, uncaughtSignal: signal, wasStopping: true,
                stateDone: false, conversationEnd: nil)
            XCTAssertEqual(s, .stopped, "stop + (\(code), signal:\(signal))")
        }
    }

    func testCleanExitWithDoneIsEndedNotCrashOrStopped() {
        // User end AND idle timeout both finalize honestly and exit 0.
        let s = ChatSessionState.afterTermination(
            status: 0, uncaughtSignal: false, wasStopping: false,
            stateDone: true, conversationEnd: "ended by user")
        XCTAssertEqual(s, .ended(reason: "ended by user"))
        let t = ChatSessionState.afterTermination(
            status: 0, uncaughtSignal: false, wasStopping: false,
            stateDone: true, conversationEnd: "conversation idle timeout")
        XCTAssertEqual(t, .ended(reason: "conversation idle timeout"))
    }

    func testCleanExitWithoutDoneIsStoppedResumable() {
        // SIGTERM mid-wait exits 0 without finalizing — resumable, not a crash.
        let s = ChatSessionState.afterTermination(
            status: 0, uncaughtSignal: false, wasStopping: false,
            stateDone: false, conversationEnd: nil)
        XCTAssertEqual(s, .stopped)
    }

    func testNonzeroExitIsCrashWithExitCode() {
        let s = ChatSessionState.afterTermination(
            status: 3, uncaughtSignal: false, wasStopping: false,
            stateDone: false, conversationEnd: nil)
        XCTAssertEqual(s, .crashed(code: 3, wasSignal: false))
    }

    func testSigkillIsCrashMarkedAsSignalNotExitCode() {
        // kill -9: terminationReason == .uncaughtSignal, status == 9 (§5.2).
        let s = ChatSessionState.afterTermination(
            status: 9, uncaughtSignal: true, wasStopping: false,
            stateDone: true, conversationEnd: "ended by user")
        XCTAssertEqual(s, .crashed(code: 9, wasSignal: true),
                       "a signal death is a crash even if state says done")
    }

    // MARK: scan merge (waiting derivation)

    func testWaitingRequiresAliveProcess() {
        // awaiting_human survives kill -9 — a dead chat must never show waiting.
        let dead = ChatSessionState.applyingScan(
            current: .crashed(code: 9, wasSignal: true),
            awaitingHuman: true, processAlive: false)
        XCTAssertEqual(dead, .crashed(code: 9, wasSignal: true))
        let stopped = ChatSessionState.applyingScan(
            current: .stopped, awaitingHuman: true, processAlive: false)
        XCTAssertEqual(stopped, .stopped)
    }

    func testRunningFlipsToWaitingAndBack() {
        let waiting = ChatSessionState.applyingScan(
            current: .running, awaitingHuman: true, processAlive: true)
        XCTAssertEqual(waiting, .waitingForHuman)
        let back = ChatSessionState.applyingScan(
            current: .waitingForHuman, awaitingHuman: false, processAlive: true)
        XCTAssertEqual(back, .running)
    }

    func testScanNeverResurrectsTerminalStates() {
        for terminal in [ChatSessionState.ended(reason: "ended by user"),
                         .crashed(code: 1, wasSignal: false), .stopped] {
            let s = ChatSessionState.applyingScan(
                current: terminal, awaitingHuman: true, processAlive: true)
            XCTAssertEqual(s, terminal)
        }
    }

    // MARK: mint

    private var tmp: URL!
    override func setUp() {
        super.setUp()
        tmp = FileManager.default.temporaryDirectory
            .appendingPathComponent("chat-mint-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
    }
    override func tearDown() {
        try? FileManager.default.removeItem(at: tmp)
        super.tearDown()
    }

    func testFlatNameSlugifiesComponentsThenJoins() {
        // Components slugified INDEPENDENTLY, then joined with literal "--":
        // slugify collapses consecutive dashes, so joining first would
        // destroy the separator (GLOSSARY "Layout (M1 interim)").
        XCTAssertEqual(
            ChatSessionMint.flatName(project: "My Proj!", section: "Ideas",
                                     title: "First Chat"),
            "my-proj--ideas--first-chat")
        let name = ChatSessionMint.flatName(project: "a--b", section: "x", title: "y")
        XCTAssertEqual(name.components(separatedBy: "--").count, 3,
                       "component content can never fake a separator")
    }

    func testMintWritesEngineDiscoveryContract() throws {
        let minted = try ChatSessionMint.mintChatDir(
            rootURL: tmp, project: "nimbus", section: "ideas", title: "brainstorm",
            workflow: "chat_ideas", firstMessage: "let's think")
        XCTAssertEqual(minted.name, "nimbus--ideas--brainstorm")
        let prompt = minted.dirURL.appendingPathComponent("initial_prompt/initial_prompt.md")
        XCTAssertEqual(try String(contentsOf: prompt, encoding: .utf8), "let's think")
        // workflow.txt is ALWAYS written: a chat dir without it resolves to
        // app_build and a shepherd relaunch would run a build debate on it.
        let wf = minted.dirURL.appendingPathComponent("workflow.txt")
        XCTAssertEqual(try String(contentsOf: wf, encoding: .utf8)
                        .trimmingCharacters(in: .whitespacesAndNewlines),
                       "chat_ideas")
    }

    func testMintCollisionSuffixesNeverOverwrites() throws {
        let first = try ChatSessionMint.mintChatDir(
            rootURL: tmp, project: "p", section: "s", title: "t",
            workflow: "chat_ideas", firstMessage: "one")
        let second = try ChatSessionMint.mintChatDir(
            rootURL: tmp, project: "p", section: "s", title: "t",
            workflow: "chat_ideas", firstMessage: "two")
        let third = try ChatSessionMint.mintChatDir(
            rootURL: tmp, project: "p", section: "s", title: "t",
            workflow: "chat_ideas", firstMessage: "three")
        XCTAssertEqual([first.name, second.name, third.name],
                       ["p--s--t", "p--s--t-2", "p--s--t-3"])
        // The originals are untouched.
        let firstPrompt = first.dirURL.appendingPathComponent("initial_prompt/initial_prompt.md")
        XCTAssertEqual(try String(contentsOf: firstPrompt, encoding: .utf8), "one")
    }
}
