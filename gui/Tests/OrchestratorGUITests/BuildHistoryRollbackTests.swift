import XCTest
@testable import OrchestratorGUI

/// OrchestratorStore's app_build git operations: structured history parsing,
/// the SAFE (forward-commit, fully-reversible) per-phase rollback, and the
/// two-commit diff. All exercised against a REAL temp git repo (the funcs are
/// nonisolated static + shell out to git, like FactoryScannerLockTests).
final class BuildHistoryRollbackTests: XCTestCase {

    private var repo: URL!

    override func setUp() {
        super.setUp()
        repo = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: repo, withIntermediateDirectories: true)
        git("init", "-q")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "t")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: repo)
        super.tearDown()
    }

    // --- helpers ---------------------------------------------------------
    @discardableResult
    private func git(_ args: String...) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["git", "-C", repo.path] + args
        let pipe = Pipe(); p.standardOutput = pipe; p.standardError = Pipe()
        try? p.run(); p.waitUntilExit()
        return String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                      encoding: .utf8) ?? ""
    }

    private func write(_ name: String, _ content: String) {
        try? content.write(to: repo.appendingPathComponent(name),
                           atomically: true, encoding: .utf8)
    }

    @discardableResult
    private func commit(_ subject: String) -> String {
        git("add", "-A")
        git("commit", "-q", "-m", subject, "--allow-empty")
        return git("rev-parse", "HEAD").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func readFile(_ name: String) -> String? {
        try? String(contentsOf: repo.appendingPathComponent(name), encoding: .utf8)
    }

    private func exists(_ name: String) -> Bool {
        FileManager.default.fileExists(atPath: repo.appendingPathComponent(name).path)
    }

    // --- structured history ---------------------------------------------
    func testHistoryParsesFieldsAndSplitsSafely() {
        write("a.txt", "1")
        // Subject with a DOUBLE space — the old "%h  %ad  %s" split broke here.
        commit("orchestrator: build_coordination  iteration 2")
        let h = OrchestratorStore.structuredBuildHistory(buildDir: repo)
        XCTAssertEqual(h.count, 1)
        XCTAssertEqual(h[0].subject, "orchestrator: build_coordination  iteration 2")
        XCTAssertFalse(h[0].sha.isEmpty)
        XCTAssertFalse(h[0].shortSha.isEmpty)
        XCTAssertTrue(h[0].date.hasPrefix("20"))
    }

    func testPhaseParsedFromIterationSubject() {
        XCTAssertEqual(
            OrchestratorStore.phase(fromSubject: "orchestrator: build_coordination iteration 3"),
            "build_coordination")
        XCTAssertEqual(
            OrchestratorStore.phase(fromSubject: "orchestrator: task_assignments iteration 12"),
            "task_assignments")
    }

    func testNonPhaseCommitsHaveNilPhase() {
        XCTAssertNil(OrchestratorStore.phase(fromSubject: "orchestrator: build repo initialized"))
        XCTAssertNil(OrchestratorStore.phase(fromSubject: "orchestrator: rolled back to abc1234"))
    }

    func testHistoryNewestFirstAndRespectsLimit() {
        for i in 1...5 { write("a.txt", "\(i)"); commit("orchestrator: p iteration \(i)") }
        let h = OrchestratorStore.structuredBuildHistory(buildDir: repo, limit: 3)
        XCTAssertEqual(h.count, 3)
        XCTAssertEqual(h[0].subject, "orchestrator: p iteration 5")  // newest first
        XCTAssertEqual(h[2].subject, "orchestrator: p iteration 3")
    }

    func testRefsCapturesRunTag() {
        write("a.txt", "1"); commit("orchestrator: p iteration 1")
        git("tag", "run-0001")
        let h = OrchestratorStore.structuredBuildHistory(buildDir: repo)
        XCTAssertTrue(h[0].refs.contains("run-0001"))
    }

    func testLaneAndMergeCommitsAreExcludedFromRollbackTargets() {
        write("base.txt", "0"); commit("orchestrator: build_coordination iteration 1")
        let main = git("branch", "--show-current").trimmingCharacters(in: .whitespacesAndNewlines)
        // Lane A: the engine merges with plain `git merge --no-edit` (no
        // --no-ff), so with the mainline unmoved this FAST-FORWARDS — lane A's
        // commit lands on the first-parent mainline. Only the subject filter
        // keeps it out of the rollback targets.
        git("checkout", "-q", "-b", "lane-a")
        write("lane_a.txt", "A"); commit("lane codex_a")
        git("checkout", "-q", main)
        git("merge", "--no-edit", "-q", "lane-a")             // fast-forward
        // Now the mainline has advanced, so lane B produces a REAL merge commit.
        git("checkout", "-q", "-b", "lane-b", main)
        write("lane_b.txt", "B"); commit("lane claude_b")
        git("checkout", "-q", main)
        write("main_progress.txt", "x"); commit("orchestrator: build_coordination iteration 2")
        git("merge", "--no-edit", "-q", "lane-b")             // real merge commit
        write("final.txt", "z"); commit("orchestrator: build_coordination iteration 3")

        let subjects = OrchestratorStore.structuredBuildHistory(buildDir: repo).map(\.subject)
        XCTAssertFalse(subjects.contains("lane codex_a"),
                       "fast-forwarded lane commit leaked: \(subjects)")
        XCTAssertFalse(subjects.contains("lane claude_b"),
                       "merged lane commit leaked: \(subjects)")
        XCTAssertFalse(subjects.contains { $0.hasPrefix("Merge branch") },
                       "merge commit leaked: \(subjects)")
        // Only the three orchestrator iteration commits are selectable.
        XCTAssertTrue(subjects.allSatisfy { $0.hasPrefix("orchestrator: ") })
        XCTAssertEqual(subjects.filter { $0.contains("iteration") }.count, 3)
    }

    func testMissingRepoReturnsEmpty() {
        let notRepo = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: notRepo, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: notRepo) }
        XCTAssertTrue(OrchestratorStore.structuredBuildHistory(buildDir: notRepo).isEmpty)
    }

    // --- rollback --------------------------------------------------------
    func testRollbackCreatesForwardCommitWithTargetTreeAndPreservesHistory() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("x.txt", "2"); let b = commit("orchestrator: p iteration 2")
        let res = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a)
        guard case .success = res else { return XCTFail("expected success, got \(res)") }
        XCTAssertEqual(readFile("x.txt"), "1")            // tree restored to A
        let head = git("rev-parse", "HEAD").trimmingCharacters(in: .whitespacesAndNewlines)
        XCTAssertNotEqual(head, a)                        // a NEW commit, not a reset
        XCTAssertNotEqual(head, b)
        XCTAssertTrue(OrchestratorStore.shaExists(buildDir: repo, b))  // B still reachable
    }

    func testRollbackRemovesFilesAddedAfterTarget() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("y.txt", "new"); commit("orchestrator: p iteration 2")
        XCTAssertTrue(exists("y.txt"))
        let res = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a)
        guard case .success = res else { return XCTFail("got \(res)") }
        // y.txt was added after A → gone from worktree AND from the new tree.
        XCTAssertFalse(exists("y.txt"), "file added after target survived rollback")
        XCTAssertFalse(git("ls-files").contains("y.txt"))
    }

    func testRollbackRestoresDeletedFile() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        try? FileManager.default.removeItem(at: repo.appendingPathComponent("x.txt"))
        commit("orchestrator: p iteration 2")   // deletes x.txt
        XCTAssertFalse(exists("x.txt"))
        let res = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a)
        guard case .success = res else { return XCTFail("got \(res)") }
        XCTAssertEqual(readFile("x.txt"), "1")  // restored
    }

    func testRollbackIsReversible() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("x.txt", "2"); commit("orchestrator: p iteration 2")
        let p = git("rev-parse", "HEAD").trimmingCharacters(in: .whitespacesAndNewlines)
        // Roll back to A, then roll back to the pre-rollback HEAD (P) → back to "2".
        guard case .success = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a) else {
            return XCTFail("first rollback failed")
        }
        XCTAssertEqual(readFile("x.txt"), "1")
        guard case .success = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: p) else {
            return XCTFail("reverse rollback failed")
        }
        XCTAssertEqual(readFile("x.txt"), "2")   // round-trip restored
    }

    func testRollbackToHeadIsNoChange() {
        write("x.txt", "1"); commit("orchestrator: p iteration 1")
        let head = git("rev-parse", "HEAD").trimmingCharacters(in: .whitespacesAndNewlines)
        let before = git("rev-list", "--count", "HEAD")
        XCTAssertEqual(OrchestratorStore.rollbackBuild(buildDir: repo, toSha: head), .noChange)
        XCTAssertEqual(git("rev-list", "--count", "HEAD"), before)  // no new commit
    }

    func testRollbackUnknownShaReturnsShaNotFound() {
        write("x.txt", "1"); commit("orchestrator: p iteration 1")
        let head = git("rev-parse", "HEAD").trimmingCharacters(in: .whitespacesAndNewlines)
        XCTAssertEqual(OrchestratorStore.rollbackBuild(buildDir: repo, toSha: "deadbeef"),
                       .shaNotFound)
        XCTAssertEqual(git("rev-parse", "HEAD").trimmingCharacters(in: .whitespacesAndNewlines),
                       head)  // HEAD unchanged
    }

    func testRollbackDirtyWorktreeRefuses() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("x.txt", "2"); commit("orchestrator: p iteration 2")
        write("x.txt", "uncommitted edit")   // dirty
        XCTAssertEqual(OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a),
                       .dirtyWorkingTree)
        XCTAssertEqual(readFile("x.txt"), "uncommitted edit")  // nothing clobbered
    }

    func testRollbackUntrackedFileRefusesToAvoidDataLoss() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("x.txt", "2"); commit("orchestrator: p iteration 2")
        // An untracked, non-ignored file has NO git backing — `git clean` would
        // destroy it irrecoverably, so a rollback must refuse.
        write("notes_untracked.txt", "precious")
        XCTAssertEqual(OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a),
                       .dirtyWorkingTree)
        XCTAssertTrue(exists("notes_untracked.txt"))
        XCTAssertEqual(readFile("notes_untracked.txt"), "precious")
    }

    func testRollbackPreservesIgnoredArtifacts() {
        write(".gitignore", "build/\n")
        commit("orchestrator: p iteration 1")
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 2")
        write("x.txt", "2"); commit("orchestrator: p iteration 3")
        // A gitignored artifact must survive rollback (clean runs without -x).
        try? FileManager.default.createDirectory(
            at: repo.appendingPathComponent("build"), withIntermediateDirectories: true)
        write("build/artifact.o", "junk")
        guard case .success = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a) else {
            return XCTFail("rollback failed")
        }
        XCTAssertTrue(exists("build/artifact.o"), "ignored artifact was deleted")
    }

    func testRollbackNonRepoReturnsNotARepo() {
        let notRepo = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: notRepo, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: notRepo) }
        XCTAssertEqual(OrchestratorStore.rollbackBuild(buildDir: notRepo, toSha: "HEAD"),
                       .notARepo)
    }

    func testRollbackCommitSucceedsWithoutAmbientGitIdentity() {
        // Simulate a repo with no usable identity: blank the local config.
        git("config", "--unset", "user.name")
        git("config", "--unset", "user.email")
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("x.txt", "2"); commit("orchestrator: p iteration 2")
        // rollbackBuild passes -c user.name/email so it must still commit.
        let res = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a)
        guard case .success = res else {
            return XCTFail("rollback should not depend on ambient git identity: \(res)")
        }
        XCTAssertEqual(readFile("x.txt"), "1")
    }

    func testWorktreeCleanAfterRollback() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        write("y.txt", "y"); commit("orchestrator: p iteration 2")
        guard case .success = OrchestratorStore.rollbackBuild(buildDir: repo, toSha: a) else {
            return XCTFail("rollback failed")
        }
        // No phantom diff for the next commit_build_state to re-commit.
        XCTAssertTrue(git("status", "--porcelain").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    // --- diff ------------------------------------------------------------
    func testDiffGroupsByFileWithAddRemove() {
        write("x.txt", "hello\n"); let a = commit("orchestrator: p iteration 1")
        write("x.txt", "goodbye\n"); let b = commit("orchestrator: p iteration 2")
        let diff = OrchestratorStore.buildDiff(buildDir: repo, from: a, to: b)
        XCTAssertEqual(diff.count, 1)
        XCTAssertEqual(diff[0].path, "x.txt")
        XCTAssertTrue(diff[0].lines.contains { $0.kind == .add && $0.text.contains("goodbye") })
        XCTAssertTrue(diff[0].lines.contains { $0.kind == .remove && $0.text.contains("hello") })
    }

    func testDiffAddedFileIsAllAdds() {
        write("x.txt", "base\n"); let a = commit("orchestrator: p iteration 1")
        write("y.txt", "line1\nline2\n"); let b = commit("orchestrator: p iteration 2")
        let diff = OrchestratorStore.buildDiff(buildDir: repo, from: a, to: b)
        let y = diff.first { $0.path == "y.txt" }
        XCTAssertNotNil(y)
        XCTAssertTrue(y!.lines.contains { $0.kind == .add })
        XCTAssertFalse(y!.lines.contains { $0.kind == .remove })
    }

    func testDiffIdenticalShasReturnsEmpty() {
        write("x.txt", "1"); let a = commit("orchestrator: p iteration 1")
        XCTAssertTrue(OrchestratorStore.buildDiff(buildDir: repo, from: a, to: a).isEmpty)
    }
}
