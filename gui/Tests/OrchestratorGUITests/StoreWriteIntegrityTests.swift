import XCTest
@testable import OrchestratorGUI

// Write-integrity regressions for OrchestratorStore (audit A-19/A-20/A-21):
//
// - A-19: editor views hold loaded-once ModelRouting snapshots; persisting a
//   whole stale snapshot silently reverted every modeled field another editor
//   wrote in between. The reload-before-mutate writers must overlay ONLY the
//   section the calling editor owns onto a fresh disk read.
// - A-20: saveProfile/applyProfile/rateProject logged success even when their
//   try? writes failed — the success line (and the exemplar launch) must be
//   gated on the write actually landing, with the failure surfaced.
// - A-21: setRuntimeInt/setRuntimeBool's missing-key insert searched for the
//   literal two characters backslash-n ("runtime:\\n") and never fired, so a
//   toggle on an older config.yaml looked saved while nothing reached disk.
//
// Fixture: the 2.7 golden-path seams (scratch defaults suite, temp
// ORCH_DIR/ORCH_ROOT, temp Application Support base, suppressed
// notifications) so a REAL store runs entirely inside temp dirs.
@MainActor
final class StoreWriteIntegrityTests: XCTestCase {
    private var root: URL!
    private var engineDir: URL!
    private var appSupport: URL!
    private var suiteName: String!
    private let fm = FileManager.default

    override func setUp() async throws {
        let base = fm.temporaryDirectory
            .appendingPathComponent("orch-write-integrity-\(UUID().uuidString)")
        root = base.appendingPathComponent("root")
        engineDir = base.appendingPathComponent("engine")
        appSupport = base.appendingPathComponent("appsupport")
        for d in [root!, engineDir!, appSupport!] {
            try fm.createDirectory(at: d, withIntermediateDirectories: true)
        }
        // Stub engine marker only — never executed by this suite.
        fm.createFile(
            atPath: engineDir.appendingPathComponent("orchestrator.py").path,
            contents: Data("# stub — never executed by this suite\n".utf8))
        setenv("ORCH_DIR", engineDir.path, 1)
        setenv("ORCH_ROOT", root.path, 1)
        suiteName = "orch-write-integrity-\(UUID().uuidString)"
        OrchestratorStore.defaults = UserDefaults(suiteName: suiteName)!
        OrchestratorStore.appSupportBaseURL = appSupport
        OrchestratorStore.suppressNotifications = true
        OrchestratorStore.scanDelayForTests = 0
        addTeardownBlock { [base] in
            try? FileManager.default.removeItem(at: base)
        }
    }

    override func tearDown() async throws {
        UserDefaults().removePersistentDomain(forName: suiteName)
        OrchestratorStore.defaults = .standard
        OrchestratorStore.appSupportBaseURL = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Orchestrator", isDirectory: true)
        OrchestratorStore.suppressNotifications = false
        OrchestratorStore.scanDelayForTests = 0
        unsetenv("ORCH_DIR")
        unsetenv("ORCH_ROOT")
    }

    // MARK: helpers

    private func makeProject(_ name: String) throws -> Project {
        let dir = root.appendingPathComponent(name, isDirectory: true)
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return Project(name: name, status: .done, currentPhase: nil,
                       currentRound: 1, nextAgent: nil, error: nil,
                       lastProcessed: nil, completedPhases: [],
                       phaseOutputs: [:], dirURL: dir)
    }

    /// chmod the directory read-only for the test body; restored in teardown
    /// so the temp tree can be deleted.
    private func makeReadOnly(_ url: URL) throws {
        try fm.setAttributes([.posixPermissions: 0o555], ofItemAtPath: url.path)
        addTeardownBlock {
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o755], ofItemAtPath: url.path)
        }
    }

    private func rootObject(at url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - A-19: reload-before-mutate routing writers

    func testStaleChainSnapshotCannotRevertFreshPhaseEdit() throws {
        let seed = """
        {"schema_version": 1, "enabled": true,
         "fallback": {"cloud_to_local": true, "local_model": "",
                      "chains": {"claude": ["claude-sonnet-4-6"]}},
         "phases": {}}
        """
        let url = engineDir.appendingPathComponent("model_routing.json")
        try seed.write(to: url, atomically: true, encoding: .utf8)
        let store = OrchestratorStore()

        // Editor A (Models & Agents) takes its loaded-once snapshot…
        var snapA = store.readModelRouting()
        // …then editor B (the Defaults grid) lands a phase edit.
        var gridCopy = store.readModelRouting()
        gridCopy.phases["tech_specs"] = PhaseRoute(codex: "gpt-5.3-codex")
        store.writeModelRouting(gridCopy)

        // Editor A now adds one fallback rung. The stale snapshot's empty
        // phases must NOT be persisted over B's edit.
        snapA.chains["codex"] = ["gpt-5.3-codex-spark"]
        store.writeModelRoutingChains(snapA.chains)

        let obj = try rootObject(at: url)
        let phases = try XCTUnwrap(obj["phases"] as? [String: Any])
        let tech = try XCTUnwrap(phases["tech_specs"] as? [String: Any],
                                 "the grid's phase edit was reverted by a stale chain write")
        XCTAssertEqual(tech["codex"] as? String, "gpt-5.3-codex")
        let fb = try XCTUnwrap(obj["fallback"] as? [String: Any])
        let chains = try XCTUnwrap(fb["chains"] as? [String: Any])
        XCTAssertEqual(chains["codex"] as? [String], ["gpt-5.3-codex-spark"])
        XCTAssertEqual(chains["claude"] as? [String], ["claude-sonnet-4-6"],
                       "the pre-existing chain must ride through untouched")
    }

    func testProjectPhaseAndChainWritersInterleaveWithoutRegression() throws {
        let store = OrchestratorStore()
        let project = try makeProject("demo")

        // Tuning editor writes one phase; fallback-overrides editor (whose
        // snapshot predates that write) then writes one chain.
        var route = PhaseRoute()
        route.rounds = 3
        route.instructions = "Focus on accessibility."
        store.writeProjectRoutingPhase("app_features", route, for: project)
        store.writeProjectRoutingChain("claude", ["claude-haiku-4-5"], for: project)

        let url = store.projectRoutingURL(project)
        var obj = try rootObject(at: url)
        var phases = try XCTUnwrap(obj["phases"] as? [String: Any])
        let feat = try XCTUnwrap(phases["app_features"] as? [String: Any],
                                 "the chain write must not revert the phase edit")
        XCTAssertEqual(feat["rounds"] as? Int, 3)
        XCTAssertEqual(feat["instructions"] as? String, "Focus on accessibility.")
        var fb = try XCTUnwrap(obj["fallback"] as? [String: Any])
        var chains = try XCTUnwrap(fb["chains"] as? [String: Any])
        XCTAssertEqual(chains["claude"] as? [String], ["claude-haiku-4-5"])

        // Clearing semantics: an all-defaults phase / empty chain deletes the
        // key (the project falls back to workflow / fleet defaults).
        store.writeProjectRoutingPhase("app_features", PhaseRoute(), for: project)
        store.writeProjectRoutingChain("claude", [], for: project)
        obj = try rootObject(at: url)
        phases = try XCTUnwrap(obj["phases"] as? [String: Any])
        XCTAssertNil(phases["app_features"], "an emptied phase must clear its key")
        fb = try XCTUnwrap(obj["fallback"] as? [String: Any])
        chains = (fb["chains"] as? [String: Any]) ?? [:]
        XCTAssertNil(chains["claude"], "an emptied chain must clear its key")
    }

    // MARK: - A-20: success is only claimed when the write landed

    func testSaveProfileFailedWriteIsSurfacedNotLoggedAsSaved() throws {
        let store = OrchestratorStore()
        let project = try makeProject("demo")
        let profiles = store.profilesDirURL
        try fm.createDirectory(at: profiles, withIntermediateDirectories: true)
        try makeReadOnly(profiles)

        store.saveProfile(named: "My Profile", from: project)
        XCTAssertFalse(store.runLog.contains("Saved profile"),
                       "a failed write must not be reported as Saved")
        XCTAssertNotNil(store.lastError)

        // Same call against a writable dir still reports success.
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: profiles.path)
        store.saveProfile(named: "My Profile", from: project)
        XCTAssertTrue(store.runLog.contains("Saved profile"))
        XCTAssertNil(store.lastError, "a successful retry clears the stale banner")
    }

    func testApplyProfileFailedWriteIsSurfacedNotLoggedAsApplied() throws {
        let store = OrchestratorStore()
        let projectDir = root.appendingPathComponent("demo", isDirectory: true)
        try fm.createDirectory(at: projectDir, withIntermediateDirectories: true)
        let profileURL = engineDir.appendingPathComponent("prof.json")
        let profileBody: [String: Any] = [
            "schema_version": 1, "enabled": true,
            "profile_name": "prof", "workflow": "app_build",
            "phases": [:] as [String: Any],
        ]
        try JSONSerialization.data(withJSONObject: profileBody)
            .write(to: profileURL)
        try makeReadOnly(projectDir)

        store.applyProfile(
            RunProfile(name: "prof", workflow: "app_build", url: profileURL),
            toProjectNamed: "demo")
        XCTAssertFalse(store.runLog.contains("Applied profile"),
                       "a failed routing write must not be reported as Applied")
        XCTAssertNotNil(store.lastError)
        XCTAssertFalse(fm.fileExists(
            atPath: projectDir.appendingPathComponent("workflow.txt").path),
            "workflow.txt must not be written after the routing write failed")
    }

    func testRateProjectFailedWriteIsSurfacedNotLoggedAsRated() throws {
        let store = OrchestratorStore()
        let project = try makeProject("demo")
        try makeReadOnly(project.dirURL)

        // verdict "good" is the dangerous path: pre-fix it also launched
        // --save-exemplar over a rating.json that never landed.
        store.rateProject(project, verdict: "good")
        XCTAssertFalse(store.runLog.contains("Rated"),
                       "a failed write must not be reported as Rated")
        XCTAssertNotNil(store.lastError)

        // Writable again: "bad" (no exemplar launch) reports honestly.
        try fm.setAttributes([.posixPermissions: 0o755],
                             ofItemAtPath: project.dirURL.path)
        store.rateProject(project, verdict: "bad")
        XCTAssertTrue(store.runLog.contains("Rated demo bad"))
        XCTAssertTrue(fm.fileExists(
            atPath: project.dirURL.appendingPathComponent("rating.json").path))
    }

    // MARK: - A-21: missing runtime key is actually inserted

    private func writeConfigFixture(_ text: String) throws -> URL {
        let url = engineDir.appendingPathComponent("config.yaml")
        try text.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    func testSetRuntimeIntInsertsMissingKeyUnderRuntimeSection() throws {
        // An older engine copy that predates the key entirely.
        let url = try writeConfigFixture("""
        models:
          claude_model: claude-sonnet-4-6
        runtime:
          poll_seconds: 5
        """)
        let store = OrchestratorStore()
        store.setRuntimeInt("phase_quality_repair_rounds", 3)
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(text.contains("runtime:\n  phase_quality_repair_rounds: 3\n"),
                      "missing key must be inserted under runtime:, got:\n\(text)")
        XCTAssertEqual(store.readRuntimeInt("phase_quality_repair_rounds",
                                            defaultValue: -1), 3)
    }

    func testSetRuntimeBoolInsertsMissingKeyUnderRuntimeSection() throws {
        let url = try writeConfigFixture("""
        runtime:
          poll_seconds: 5
        """)
        let store = OrchestratorStore()
        store.setRuntimeBool("phase_quality_gates_enabled", true)
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(text.contains("runtime:\n  phase_quality_gates_enabled: true\n"),
                      "missing key must be inserted under runtime:, got:\n\(text)")
        XCTAssertTrue(store.readRuntimeBool("phase_quality_gates_enabled",
                                            defaultValue: false))
    }

    func testSetRuntimeSurfacesErrorWhenNoRuntimeSectionExists() throws {
        let url = try writeConfigFixture("""
        models:
          claude_model: claude-sonnet-4-6
        """)
        let before = try String(contentsOf: url, encoding: .utf8)
        let store = OrchestratorStore()
        store.setRuntimeInt("phase_quality_repair_rounds", 3)
        XCTAssertEqual(try String(contentsOf: url, encoding: .utf8), before,
                       "no insert point — the file must not be rewritten unchanged")
        XCTAssertTrue((store.lastError ?? "").contains("phase_quality_repair_rounds"),
                      "the silent no-op must surface, like setAgentEnabled does")
    }

    // MARK: - A-55: chat-dir writers surface failure (and merge, atomically)

    func testSetChatModelOverrideMergesExistingKeysAndSurfacesFailure() throws {
        let store = OrchestratorStore()
        let chatDir = root.appendingPathComponent("demo--main--idea", isDirectory: true)
        try fm.createDirectory(at: chatDir, withIntermediateDirectories: true)
        let url = chatDir.appendingPathComponent("model_routing.json")
        try """
        {"schema_version": 1, "enabled": true, "_note": "engine key survives",
         "phases": {"chat": {"codex": "gpt-5.4-mini"}}}
        """.write(to: url, atomically: true, encoding: .utf8)

        store.setChatModelOverride("demo--main--idea", phaseKey: "chat",
                                   agent: "claude", model: "claude-haiku-4-5")
        var obj = try rootObject(at: url)
        XCTAssertEqual(obj["_note"] as? String, "engine key survives")
        var chat = try XCTUnwrap(
            (obj["phases"] as? [String: Any])?["chat"] as? [String: Any])
        XCTAssertEqual(chat["claude"] as? String, "claude-haiku-4-5")
        XCTAssertEqual(chat["codex"] as? String, "gpt-5.4-mini",
                       "another agent's override must ride through the merge")
        XCTAssertNil(store.lastError)

        // nil model clears just that agent's override.
        store.setChatModelOverride("demo--main--idea", phaseKey: "chat",
                                   agent: "claude", model: nil)
        obj = try rootObject(at: url)
        chat = try XCTUnwrap(
            (obj["phases"] as? [String: Any])?["chat"] as? [String: Any])
        XCTAssertNil(chat["claude"])
        XCTAssertEqual(chat["codex"] as? String, "gpt-5.4-mini")

        // Pre-fix, a failed write was swallowed: the chip stayed "pending"
        // forever with no error. Now it must surface.
        try makeReadOnly(chatDir)
        store.setChatModelOverride("demo--main--idea", phaseKey: "chat",
                                   agent: "claude", model: "opus")
        XCTAssertNotNil(store.lastError, "a lost override must surface")
    }

    func testRequestChatRetryWritesPayloadAndSurfacesFailure() throws {
        let store = OrchestratorStore()
        let chatDir = root.appendingPathComponent("demo--main--idea", isDirectory: true)
        let apprDir = chatDir.appendingPathComponent("approvals", isDirectory: true)
        try fm.createDirectory(at: chatDir, withIntermediateDirectories: true)

        store.requestChatRetry("demo--main--idea", phaseKey: "chat",
                               agent: "claude", model: "opus")
        let obj = try rootObject(at: apprDir.appendingPathComponent("chat.retry"))
        XCTAssertEqual(obj["agent"] as? String, "claude")
        XCTAssertEqual(obj["model"] as? String, "opus")
        XCTAssertNil(store.lastError)

        try makeReadOnly(apprDir)
        store.requestChatRetry("demo--main--idea", phaseKey: "chat",
                               agent: "claude", model: "opus")
        XCTAssertNotNil(store.lastError, "a silently-lost retry must surface")
    }

    // MARK: - A-58: writeRunConfig is a read-merge like the file's other writers

    func testWriteRunConfigPreservesSensitivityAndSituationAndClearsDefaults() throws {
        let store = OrchestratorStore()
        let project = try makeProject("demo")
        let url = project.dirURL.appendingPathComponent("run_config.json")
        let seed: [String: Any] = ["sensitivity": "private",
                                   "situation": ["id": "sit-1"],
                                   "autonomy": "human_in_the_loop"]
        try JSONSerialization.data(withJSONObject: seed).write(to: url)

        // Update completeness/stop; revert autonomy to its default.
        store.writeRunConfig(project: "demo", autonomy: "fully_autonomous",
                             completeness: "mvp", stopAfter: "tech_specs")
        let obj = try rootObject(at: url)
        XCTAssertEqual(obj["sensitivity"] as? String, "private",
                       "the privacy floor must survive a run-config write")
        XCTAssertEqual((obj["situation"] as? [String: Any])?["id"] as? String,
                       "sit-1")
        XCTAssertEqual(obj["completeness"] as? String, "mvp")
        XCTAssertEqual(obj["stop_after_phase"] as? String, "tech_specs")
        XCTAssertNil(obj["autonomy"],
                     "a default value must clear its earlier key, not linger")
    }

    // MARK: - A-59: direct routing saves can invalidate the TTL cache

    func testInvalidateRoutingCacheMakesDirectSavesVisibleImmediately() throws {
        let url = engineDir.appendingPathComponent("model_routing.json")
        try #"{"schema_version": 1, "enabled": true, "phases": {}}"#
            .write(to: url, atomically: true, encoding: .utf8)
        let store = OrchestratorStore()
        XCTAssertTrue(store.readModelRouting().phases.isEmpty)   // warm the cache

        // The routing grid's Apply path: a direct save(to:) behind the store.
        var grid = ModelRouting.load(from: url)
        grid.phases["tech_specs"] = PhaseRoute(codex: "gpt-5.3-codex")
        grid.save(to: url)

        XCTAssertTrue(store.readModelRouting().phases.isEmpty,
                      "precondition: within the TTL the cache serves the pre-Apply copy")
        store.invalidateRoutingCache(at: store.modelRoutingURL)
        XCTAssertEqual(store.readModelRouting().phases["tech_specs"]?.codex,
                       "gpt-5.3-codex",
                       "after invalidation the next read must hit disk — else a "
                       + "reload-before-mutate writer would revert the Apply")
    }
}
