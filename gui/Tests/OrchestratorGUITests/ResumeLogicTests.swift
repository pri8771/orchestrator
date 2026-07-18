import XCTest
@testable import OrchestratorGUI

/// Crashed-run detection + resume offers (ResumeLogic.swift +
/// FactoryScanner.staleLockNames). Pure statics with injected pid probes and
/// temp dirs — never instantiates OrchestratorStore (suite convention).
final class ResumeLogicTests: XCTestCase {

    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    // MARK: - Staleness (shepherd locked() parity)

    func testDeadPidLockIsStale() {
        let locks = ["demo": AppLockInfo(pid: 4242, since: t0)]
        XCTAssertEqual(FactoryScanner.staleLockNames(in: locks, isPidAlive: { _ in false }),
                       ["demo"])
    }

    func testLivePidLockIsNotStale() {
        // The UI must never call a live run crashed.
        let locks = ["demo": AppLockInfo(pid: 4242, since: t0)]
        XCTAssertEqual(FactoryScanner.staleLockNames(in: locks, isPidAlive: { _ in true }),
                       [])
    }

    func testNilPidLockIsStale() {
        // shepherd locked(): a pid-less lock is not locked.
        let locks = ["demo": AppLockInfo(pid: nil, since: t0)]
        XCTAssertEqual(FactoryScanner.staleLockNames(in: locks, isPidAlive: { _ in true }),
                       ["demo"])
    }

    func testOwnProcessAliveWithRealChecker() {
        // Un-mocked smoke test of the kill(2) wrapper: our own pid is alive…
        let own = ["me": AppLockInfo(pid: getpid(), since: t0)]
        XCTAssertEqual(FactoryScanner.staleLockNames(in: own), [])
        // …and a spawned-then-reaped child is dead.
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        try? proc.run()
        proc.waitUntilExit()
        let dead = ["gone": AppLockInfo(pid: proc.processIdentifier, since: t0)]
        XCTAssertEqual(FactoryScanner.staleLockNames(in: dead), ["gone"])
    }

    // MARK: - Offer candidates (exclusion table)

    private func offers(stale: Set<String> = ["a"],
                        locks: [String: AppLockInfo]? = nil,
                        autorun: Set<String> = [],
                        doneOrMissing: Set<String> = [],
                        guiOwned: Set<String> = [],
                        queued: Set<String> = [],
                        stopped: Set<String> = []) -> [ResumeOffer] {
        ResumeAdvisor.candidates(
            staleLocks: stale,
            locks: locks ?? ["a": AppLockInfo(pid: 4242, since: t0)],
            autorunDisabled: autorun,
            doneOrMissing: doneOrMissing,
            guiOwnedLive: guiOwned,
            queuedOrLaunching: queued,
            manuallyStopped: stopped)
    }

    func testCrashedInProgressAppIsOffered() {
        let out = offers()
        XCTAssertEqual(out.map(\.name), ["a"])
        XCTAssertEqual(out.first?.deadPid, 4242)
        XCTAssertEqual(out.first?.since, t0)
    }

    func testAutorunDisabledNeverOffered() {
        XCTAssertTrue(offers(autorun: ["a"]).isEmpty)
    }

    func testDoneProjectNotOffered() {
        // Leftover lock on a done app is cleanup, not resume.
        XCTAssertTrue(offers(doneOrMissing: ["a"]).isEmpty)
    }

    func testQueuedOrLaunchingNotOffered() {
        XCTAssertTrue(offers(queued: ["a"]).isEmpty)
    }

    func testGuiOwnedLiveProcessNotOffered() {
        XCTAssertTrue(offers(guiOwned: ["a"]).isEmpty)
    }

    func testManualStopSuppressesOffer() {
        // stopRun's SIGTERM→SIGKILL grace must not read as a crash.
        XCTAssertTrue(offers(stopped: ["a"]).isEmpty)
    }

    func testNilPidOfferCarriesNilDeadPid() {
        let out = offers(locks: ["a": AppLockInfo(pid: nil, since: t0)])
        XCTAssertEqual(out.count, 1)
        XCTAssertNil(out.first?.deadPid)
    }

    func testOffersSortedByName() {
        let out = offers(stale: ["b", "a"],
                         locks: ["a": AppLockInfo(pid: 1, since: t0),
                                 "b": AppLockInfo(pid: 2, since: t0)])
        XCTAssertEqual(out.map(\.name), ["a", "b"])
    }

    // MARK: - Settling (mid-write flicker guard)

    func testStaleMustSettleBeforeOffer() {
        let seen = ResumeAdvisor.settledFirstSeen(previous: [:], nowStale: ["a"], now: t0)
        XCTAssertEqual(seen["a"], t0)
        XCTAssertFalse(ResumeAdvisor.settled(seen, now: t0).contains("a"))
        XCTAssertTrue(ResumeAdvisor.settled(seen, now: t0.addingTimeInterval(3.6)).contains("a"))
    }

    func testFirstSeenPrunedWhenLockGoesLive() {
        // A re-crash restarts the settling window.
        let seen = ResumeAdvisor.settledFirstSeen(previous: ["a": t0], nowStale: [], now: t0)
        XCTAssertTrue(seen.isEmpty)
    }

    // MARK: - Launch routing (the 240s self-busy carve-out)

    func testImmediateLaunchWhenOnlySelfAppearsRunning() {
        XCTAssertTrue(ResumeAdvisor.immediateLaunchAllowed(
            resuming: "a", runningProjectNames: ["a"], launchingName: nil, queueEmpty: true))
    }

    func testQueuesBehindAnotherRunningProject() {
        XCTAssertFalse(ResumeAdvisor.immediateLaunchAllowed(
            resuming: "a", runningProjectNames: ["a", "other"], launchingName: nil,
            queueEmpty: true))
    }

    func testQueuesWhileLaunchingOrQueueNonEmpty() {
        XCTAssertFalse(ResumeAdvisor.immediateLaunchAllowed(
            resuming: "a", runningProjectNames: [], launchingName: "b", queueEmpty: true))
        XCTAssertFalse(ResumeAdvisor.immediateLaunchAllowed(
            resuming: "a", runningProjectNames: [], launchingName: nil, queueEmpty: false))
    }

    // MARK: - Banner copy (never lie)

    func testBannerTextDeadPidNamesThePid() {
        let text = ResumeAdvisor.bannerText(deadPid: 4242, since: t0,
                                            shepherdActive: false,
                                            now: t0.addingTimeInterval(300))
        XCTAssertTrue(text.contains("pid 4242"))
        XCTAssertTrue(text.contains("5 minutes ago"))
        XCTAssertFalse(text.contains("can't verify"))
    }

    func testBannerTextNilPidAdmitsUncertainty() {
        let text = ResumeAdvisor.bannerText(deadPid: nil, since: t0,
                                            shepherdActive: false, now: t0)
        XCTAssertTrue(text.contains("names no pid"))
        XCTAssertFalse(text.contains("pid 0"))   // never fabricates one
    }

    func testBannerTextMentionsShepherdOnlyWhenActive() {
        let on = ResumeAdvisor.bannerText(deadPid: 1, since: t0,
                                          shepherdActive: true, now: t0)
        let off = ResumeAdvisor.bannerText(deadPid: 1, since: t0,
                                           shepherdActive: false, now: t0)
        XCTAssertTrue(on.contains("Shepherd"))
        XCTAssertFalse(off.contains("Shepherd"))
    }

    // MARK: - End-to-end file-shaped + shepherd parity

    func testScanLocksFeedsStaleness() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent(".orch-locks"),
            withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try "pid=4242 host=box started=2026-07-15 09:30:00\n".write(
            to: root.appendingPathComponent(".orch-locks/demo.lock"),
            atomically: true, encoding: .utf8)
        let locks = FactoryScanner.scanLocks(rootURL: root)
        let stale = FactoryScanner.staleLockNames(in: locks, isPidAlive: { _ in false })
        XCTAssertEqual(stale, ["demo"])
        // The parsed started= stamp survives into the offer.
        let offer = ResumeAdvisor.candidates(
            staleLocks: stale, locks: locks, autorunDisabled: [], doneOrMissing: [],
            guiOwnedLive: [], queuedOrLaunching: [], manuallyStopped: []).first
        let comps = Calendar.current.dateComponents([.hour, .minute],
                                                    from: offer?.since ?? .distantPast)
        XCTAssertEqual(comps.hour, 9)
        XCTAssertEqual(comps.minute, 30)
    }

    func testShepherdLockedParity() throws {
        // Pin GUI/shepherd agreement: `shepherd.sh --check-lock` (the locked()
        // test hook) must agree with staleLockNames for a dead-pid lock and a
        // live-pid lock, so the two dead-pid rules can't silently drift.
        let repo = URL(fileURLWithPath: #filePath)          // …/gui/Tests/OrchestratorGUITests/x.swift
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let shepherd = repo.appendingPathComponent("shepherd.sh")
        try XCTSkipUnless(FileManager.default.fileExists(atPath: shepherd.path),
                          "shepherd.sh not found (packaged test run)")
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent(".orch-locks"),
            withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let reaped = Process()
        reaped.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        try reaped.run()
        reaped.waitUntilExit()
        try "pid=\(reaped.processIdentifier)\n".write(
            to: root.appendingPathComponent(".orch-locks/dead.lock"),
            atomically: true, encoding: .utf8)
        try "pid=\(getpid())\n".write(
            to: root.appendingPathComponent(".orch-locks/live.lock"),
            atomically: true, encoding: .utf8)

        func shepherdSaysLocked(_ app: String) throws -> Bool {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = [shepherd.path, "--check-lock", app]
            p.environment = ProcessInfo.processInfo.environment
                .merging(["ORCH_ROOT": root.path]) { _, new in new }
            try p.run()
            p.waitUntilExit()
            return p.terminationStatus == 0
        }

        let stale = FactoryScanner.staleLockNames(in: FactoryScanner.scanLocks(rootURL: root))
        XCTAssertTrue(stale.contains("dead"))
        XCTAssertFalse(stale.contains("live"))
        XCTAssertFalse(try shepherdSaysLocked("dead"))   // shepherd: not locked
        XCTAssertTrue(try shepherdSaysLocked("live"))    // shepherd: locked
    }
}
