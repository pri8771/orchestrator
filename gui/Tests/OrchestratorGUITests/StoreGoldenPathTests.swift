import XCTest
import Combine
import Darwin
@testable import OrchestratorGUI

// V3 board 2.7: the first golden-path suite that constructs a REAL
// OrchestratorStore — against a temp workspace, a stub engine dir, a
// scratch UserDefaults suite, and a temp Application Support base, via
// the four 2.7 seams. THIS SUITE IS THE HARD PREREQUISITE OF 8.1: every
// store-split PR must keep it green before and after each extraction.
//
// Escape honesty: teardown proves the AUDITED escape paths didn't fire
// (standard-defaults keys, the real Application Support/Orchestrator
// chat targets, notification auth). An in-process syscall-level
// guarantee doesn't exist; the 2.7 escape audit is the enumeration this
// proof rests on. The only child process any test spawns is a throwaway
// /bin/sleep; the store's read-only pgrep shepherd probe is accepted by
// design (never assert on shepherdActive).
@MainActor
final class StoreGoldenPathTests: XCTestCase {
    private var root: URL!
    private var engineDir: URL!
    private var appSupport: URL!
    private var suiteName: String!
    private var stores: [OrchestratorStore] = []

    private var realEnginePaused: Any?
    private var realWorkspaceRoot: Any?
    private var realChatSig: String = ""

    private var realChatTargets: [URL] {
        let base = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Orchestrator", isDirectory: true)
        return [base.appendingPathComponent("chat_history.json"),
                base.appendingPathComponent("chat_history", isDirectory: true)]
    }

    private func chatSignature() -> String {
        let fm = FileManager.default
        return realChatTargets.map { url in
            let attrs = try? fm.attributesOfItem(atPath: url.path)
            let mtime = (attrs?[.modificationDate] as? Date)?
                .timeIntervalSince1970 ?? -1
            let count = (try? fm.contentsOfDirectory(atPath: url.path).count) ?? -1
            return "\(url.lastPathComponent):\(mtime):\(count)"
        }.joined(separator: "|")
    }

    override func setUp() async throws {
        let base = FileManager.default.temporaryDirectory
            .appendingPathComponent("orch-2.7-\(UUID().uuidString)")
        root = base.appendingPathComponent("root")
        engineDir = base.appendingPathComponent("engine")
        appSupport = base.appendingPathComponent("appsupport")
        for d in [root!, engineDir!, appSupport!] {
            try FileManager.default.createDirectory(
                at: d, withIntermediateDirectories: true)
        }
        // A stub engine marker makes engineAvailable=true; tests for the
        // refusal path remove it. Never a runnable engine.
        FileManager.default.createFile(
            atPath: engineDir.appendingPathComponent("orchestrator.py").path,
            contents: Data("# stub — never executed by this suite\n".utf8))
        setenv("ORCH_DIR", engineDir.path, 1)
        setenv("ORCH_ROOT", root.path, 1)
        suiteName = "orch-goldenpath-\(UUID().uuidString)"
        OrchestratorStore.defaults = UserDefaults(suiteName: suiteName)!
        OrchestratorStore.appSupportBaseURL = appSupport
        OrchestratorStore.suppressNotifications = true
        OrchestratorStore.scanDelayForTests = 0

        realEnginePaused = UserDefaults.standard.object(forKey: "enginePaused")
        realWorkspaceRoot = UserDefaults.standard.object(forKey: "workspaceRoot")
        realChatSig = chatSignature()
    }

    override func tearDown() async throws {
        // Escape proof first, while the test's state is still visible.
        XCTAssertEqual(
            UserDefaults.standard.object(forKey: "enginePaused") as? Bool,
            realEnginePaused as? Bool, "standard defaults enginePaused escaped")
        XCTAssertEqual(
            UserDefaults.standard.object(forKey: "workspaceRoot") as? String,
            realWorkspaceRoot as? String, "standard defaults workspaceRoot escaped")
        XCTAssertEqual(chatSignature(), realChatSig,
                       "the real Application Support chat targets changed")
        for s in stores {
            XCTAssertFalse(s.notifAuthRequested,
                           "a real notification authorization was requested")
            XCTAssertTrue(s.rootURL.path.hasPrefix(root.path))
            XCTAssertTrue(s.orchDirURL.path.hasPrefix(engineDir.path))
        }
        stores = []
        UserDefaults().removePersistentDomain(forName: suiteName)
        OrchestratorStore.defaults = .standard
        OrchestratorStore.appSupportBaseURL = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Orchestrator", isDirectory: true)
        OrchestratorStore.suppressNotifications = false
        OrchestratorStore.scanDelayForTests = 0
        AppDelegate.runsActive = false
        unsetenv("ORCH_DIR")
        unsetenv("ORCH_ROOT")
    }

    private func makeStore() -> OrchestratorStore {
        let s = OrchestratorStore()
        stores.append(s)   // never reuse across tests (detached SIGKILL task)
        return s
    }

    /// One refresh cycle, synchronized on the unconditional per-apply
    /// heartbeat ($orchestratorRunning emits on every set; most other
    /// applies are `!=`-guarded and can stay silent).
    private func refreshAndWait(_ store: OrchestratorStore,
                                applies: Int = 1,
                                calls: (() -> Void)? = nil) {
        let exp = expectation(description: "refresh apply x\(applies)")
        exp.expectedFulfillmentCount = applies
        exp.assertForOverFulfill = false
        let sink = store.$orchestratorRunning.dropFirst()
            .sink { _ in exp.fulfill() }
        if let calls { calls() } else { store.refresh() }
        wait(for: [exp], timeout: 10)
        sink.cancel()
    }

    private func mintProject(_ name: String,
                             state: [String: Any] = [:]) throws {
        let dir = root.appendingPathComponent(name)
        try FileManager.default.createDirectory(
            at: dir.appendingPathComponent("initial_prompt"),
            withIntermediateDirectories: true)
        try Data("build \(name)\n".utf8).write(
            to: dir.appendingPathComponent("initial_prompt/initial_prompt.md"))
        var st: [String: Any] = ["current_phase": NSNull(),
                                 "current_round": 0,
                                 "completed_phases": [String](),
                                 "phase_outputs": [String: String]()]
        st.merge(state) { _, new in new }
        let data = try JSONSerialization.data(withJSONObject: st)
        let stateURL = dir.appendingPathComponent("agent_state.json")
        try data.write(to: stateURL)
        // A fresh state mtime reads as a LIVE run (the loader's 240s
        // heartbeat window) — age it so a minted project is honestly idle.
        try FileManager.default.setAttributes(
            [.modificationDate: Date(timeIntervalSinceNow: -600)],
            ofItemAtPath: stateURL.path)
    }

    /// Force the load path: "home" is the default key, so switching to it
    /// directly is a no-op by design.
    private func reloadHomeChat(_ store: OrchestratorStore) {
        store.switchChat(to: "goldenpath-detour")
        store.switchChat(to: "home")
    }

    // MARK: - Discovery / refresh

    func testRefreshDiscoversMintedProjects() throws {
        try mintProject("alpha")
        try mintProject("bravo")
        let store = makeStore()
        refreshAndWait(store)
        XCTAssertEqual(store.projects.map(\.name).sorted(),
                       ["alpha", "bravo"])
        XCTAssertFalse(store.projects[0].running)
    }

    func testOverlappingRefreshCoalescesAndBothApply(){
        let store = makeStore()
        // Second call while the first is in flight sets refreshPending;
        // the pending pass chains from the first apply — two heartbeats.
        refreshAndWait(store, applies: 2) {
            store.refresh()
            store.refresh()
        }
    }

    func testWatchdogRecoversAWedgedRefresh() {
        let store = makeStore()
        store.watchdogSeconds = 0.3
        OrchestratorStore.scanDelayForTests = 1.5
        let warned = expectation(description: "watchdog warning")
        let sink = store.$runLog.dropFirst().sink { log in
            if log.contains("didn't finish within") { warned.fulfill() }
        }
        store.refresh()
        wait(for: [warned], timeout: 5)
        sink.cancel()
        // The wedged scan's LATE completion is a stale-generation no-op;
        // a fresh refresh must succeed end-to-end afterwards (§12.1:
        // loading must end).
        OrchestratorStore.scanDelayForTests = 0
        refreshAndWait(store)
    }

    // MARK: - Stop

    private func writeLock(_ name: String, pid: Int32) throws {
        let locks = root.appendingPathComponent(".orch-locks")
        try FileManager.default.createDirectory(
            at: locks, withIntermediateDirectories: true)
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let body = "pid=\(pid)\nstarted=\(fmt.string(from: Date()))\n"
        try Data(body.utf8).write(
            to: locks.appendingPathComponent("\(name).lock"))
    }

    func testStopRunTerminatesTheLockedLivePid() throws {
        try mintProject("charlie")
        // The ONLY child this suite spawns: a throwaway sleep whose pid
        // WE wrote into the lock — stopRun can never signal a stranger.
        let child = Process()
        child.executableURL = URL(fileURLWithPath: "/bin/sleep")
        child.arguments = ["300"]
        let died = expectation(description: "child terminated")
        child.terminationHandler = { _ in died.fulfill() }
        try child.run()
        addTeardownBlock {
            if child.isRunning { child.terminate() }
            child.waitUntilExit()
        }
        try writeLock("charlie", pid: child.processIdentifier)
        let store = makeStore()
        refreshAndWait(store)
        XCTAssertEqual(store.appLocks["charlie"]?.pid,
                       child.processIdentifier)
        store.stopRun("charlie")
        wait(for: [died], timeout: 5)   // SIGTERM path, not the 5s SIGKILL
    }

    func testStaleDeadPidLockIsClearedHonestly() throws {
        try mintProject("delta")
        // Guaranteed-dead pid: above macOS's PID ceiling (99999), and
        // probe-then-use with the same syscall stopRun uses, so the test
        // stays valid even if the ceiling assumption ever changes.
        var pid: Int32 = 400_000
        while !(kill(pid, 0) == -1 && errno == ESRCH) { pid += 1 }
        try writeLock("delta", pid: pid)
        let store = makeStore()
        refreshAndWait(store)
        XCTAssertEqual(store.appLocks["delta"]?.pid, pid)
        store.stopRun("delta")
        XCTAssertTrue(store.runLog.contains("no longer running"),
                      "the stale-lock line must be surfaced")
        XCTAssertFalse(FileManager.default.fileExists(
            atPath: root.appendingPathComponent(".orch-locks/delta.lock").path),
            "the stale lock must be cleared — a dead pid must never pin a lane")
        refreshAndWait(store)
        XCTAssertFalse(store.projects.first { $0.name == "delta" }?.running
                       ?? false, "a dead-pid lock must never render running")
    }

    // MARK: - Chat history

    func testChatHistoryRoundTripsAcrossStoreInstances() {
        let one = makeStore()
        one.chatMessages = [ConciergeMessage(role: .user, text: "hello keel")]
        XCTAssertTrue(FileManager.default.fileExists(
            atPath: appSupport.appendingPathComponent(
                "chat_history/chat-home.json").path))
        let two = makeStore()
        reloadHomeChat(two)
        XCTAssertEqual(two.chatMessages.map(\.text), ["hello keel"])
    }

    func testCorruptChatHistoryDegradesToEmptyWithoutCrash() throws {
        let dir = appSupport.appendingPathComponent("chat_history")
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        try Data("{corrupt".utf8).write(
            to: dir.appendingPathComponent("chat-home.json"))
        let store = makeStore()
        reloadHomeChat(store)
        XCTAssertEqual(store.chatMessages, [])
    }

    func testLegacyMigrationStaysInsideTheInjectedBase() throws {
        // A legacy chat_history.json in the TEST base migrates into the
        // TEST per-chat dir; teardown proves the real one untouched.
        let legacy = [ConciergeMessage(role: .user, text: "legacy line")]
        let data = try JSONEncoder().encode(legacy)
        try data.write(to: appSupport.appendingPathComponent("chat_history.json"))
        let store = makeStore()
        reloadHomeChat(store)
        XCTAssertEqual(store.chatMessages.map(\.text), ["legacy line"])
    }

    // MARK: - Launch refusal / defaults isolation

    func testRunOrQueueSurfacesEngineUnavailable() throws {
        try mintProject("echo")
        try FileManager.default.removeItem(
            at: engineDir.appendingPathComponent("orchestrator.py"))
        let store = makeStore()
        XCTAssertFalse(store.engineAvailable)
        store.runOrQueue("echo")
        XCTAssertTrue(store.runLog.contains("Cannot launch"),
                      "an unavailable engine must refuse loudly, not no-op")
    }

    func testDefaultsSeamIsolatesEnginePaused() {
        OrchestratorStore.defaults.set(true, forKey: "enginePaused")
        let store = makeStore()
        XCTAssertTrue(store.enginePaused,
                      "the store must read the injected suite")
        store.enginePaused = false
        XCTAssertEqual(OrchestratorStore.defaults.bool(forKey: "enginePaused"),
                       false, "writes land in the injected suite")
        // teardown proves the STANDARD suite never changed
    }
}
