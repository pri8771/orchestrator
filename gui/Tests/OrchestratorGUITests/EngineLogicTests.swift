import XCTest
@testable import OrchestratorGUI

// Tests for the pure GUI↔engine bridge logic in EngineLogic.swift:
// verify_results.json parsing, approval decision files, blocked_conflict
// parsing, and the engine-dir fallback precedence.

// MARK: - verify_results.json parsing

final class VerifyResultsParserTests: XCTestCase {

    private func data(_ s: String) -> Data { Data(s.utf8) }

    func testValidArrayParsesAllFieldsAndLatestIsLast() {
        let json = """
        [
          {"schema_version": 1, "timestamp": "2026-07-01 10:00:00", "phase": "build_coordination",
           "prompt_hash": "abc", "attempt": 0, "repair_attempt": false, "ran": true, "ok": false,
           "status": "failed", "tool": "xcodebuild", "scheme": "MyApp",
           "summary": "Build failed", "errors": "error: no such module 'Foo'"},
          {"schema_version": 1, "timestamp": "2026-07-01 10:05:00", "phase": "build_coordination",
           "prompt_hash": "abc", "attempt": 1, "repair_attempt": true, "ran": true, "ok": true,
           "status": "verified", "tool": "xcodebuild", "scheme": "MyApp",
           "summary": "Build succeeded", "errors": ""}
        ]
        """
        let records = VerifyResultsParser.parse(data(json))
        XCTAssertEqual(records.count, 2)

        let latest = VerifyResultsParser.latest(records)
        XCTAssertNotNil(latest)
        XCTAssertEqual(latest?.status, "verified")
        XCTAssertTrue(latest?.ok ?? false)
        XCTAssertEqual(latest?.tool, "xcodebuild")
        XCTAssertEqual(latest?.scheme, "MyApp")
        XCTAssertEqual(latest?.summary, "Build succeeded")
        XCTAssertEqual(latest?.attempt, 1)
        XCTAssertTrue(latest?.repairAttempt ?? false)
        XCTAssertEqual(latest?.promptHash, "abc")
        XCTAssertEqual(latest?.timestamp, "2026-07-01 10:05:00")
        XCTAssertEqual(latest?.phase, "build_coordination")
        XCTAssertEqual(latest?.statusLabel, "VERIFIED")

        XCTAssertEqual(records[0].status, "failed")
        XCTAssertEqual(records[0].errors, "error: no such module 'Foo'")
        XCTAssertEqual(VerifyResultsParser.repairAttemptCount(records), 1)
    }

    func testEmptyDataAndEmptyArrayReturnEmpty() {
        XCTAssertEqual(VerifyResultsParser.parse(Data()), [])
        XCTAssertEqual(VerifyResultsParser.parse(data("[]")), [])
        XCTAssertNil(VerifyResultsParser.latest([]))
        XCTAssertEqual(VerifyResultsParser.repairAttemptCount([]), 0)
    }

    func testMalformedJSONReturnsEmpty() {
        XCTAssertEqual(VerifyResultsParser.parse(data("{not json")), [])
        XCTAssertEqual(VerifyResultsParser.parse(data("42")), [])
        // A top-level object (not an array) is unusable too.
        XCTAssertEqual(VerifyResultsParser.parse(data("{\"ok\": true}")), [])
    }

    func testNonDictEntriesAreSkipped() {
        let records = VerifyResultsParser.parse(data(
            "[\"junk\", {\"ran\": true, \"ok\": true, \"status\": \"verified\"}, 7]"))
        XCTAssertEqual(records.count, 1)
        XCTAssertEqual(records[0].status, "verified")
    }

    func testMissingFieldsFallBackAndStatusIsDerived() {
        // No status recorded: derive from ran/ok (verify.py: verification_status).
        let verified = VerifyResultsParser.parse(data("[{\"ran\": true, \"ok\": true}]"))
        XCTAssertEqual(verified.first?.status, "verified")
        let failed = VerifyResultsParser.parse(data("[{\"ran\": true, \"ok\": false}]"))
        XCTAssertEqual(failed.first?.status, "failed")
        let unverified = VerifyResultsParser.parse(data("[{}]"))
        XCTAssertEqual(unverified.first?.status, "unverified")
        XCTAssertEqual(unverified.first?.tool, "none")
        XCTAssertEqual(unverified.first?.summary, "")
        XCTAssertEqual(unverified.first?.errors, "")
        XCTAssertEqual(unverified.first?.attempt, 0)
        XCTAssertFalse(unverified.first?.repairAttempt ?? true)
    }

    func testMissingFileReturnsEmpty() {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("orchgui-tests-\(UUID().uuidString)")
            .appendingPathComponent("verify_results.json")
        XCTAssertEqual(VerifyResultsParser.parse(fileAt: url), [])
    }

    func testFileRoundTrip() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("orchgui-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("verify_results.json")
        try data("[{\"ran\": true, \"ok\": false, \"status\": \"failed\", \"summary\": \"boom\"}]")
            .write(to: url)
        let records = VerifyResultsParser.parse(fileAt: url)
        XCTAssertEqual(records.count, 1)
        XCTAssertEqual(records[0].summary, "boom")
    }
}

// MARK: - Approval decision files (spec §3.1)

final class ApprovalFilesTests: XCTestCase {

    func testDecisionFileNames() {
        XCTAssertEqual(ApprovalDecision.approve.fileName(phase: "tech_specs"), "tech_specs.ok")
        XCTAssertEqual(ApprovalDecision.editAndApprove.fileName(phase: "tech_specs"), "tech_specs.edit")
        XCTAssertEqual(ApprovalDecision.requestChanges.fileName(phase: "tech_specs"), "tech_specs.changes")
    }

    func testURLIsUnderApprovalsDir() {
        let proj = URL(fileURLWithPath: "/tmp/workspace/my-app", isDirectory: true)
        let url = ApprovalFiles.url(projectDir: proj, phase: "app_features", decision: .approve)
        XCTAssertEqual(url.path, "/tmp/workspace/my-app/approvals/app_features.ok")
    }

    func testWriteCreatesApprovalsDirAndBody() throws {
        let proj = FileManager.default.temporaryDirectory
            .appendingPathComponent("orchgui-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: proj, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: proj) }

        // Edit & Approve: body is the replacement phase output.
        let edited = try ApprovalFiles.write(projectDir: proj, phase: "tech_specs",
                                             decision: .editAndApprove, body: "# New output\n")
        XCTAssertEqual(edited.lastPathComponent, "tech_specs.edit")
        XCTAssertEqual(try String(contentsOf: edited, encoding: .utf8), "# New output\n")

        // Request Changes: body is the human feedback.
        let changes = try ApprovalFiles.write(projectDir: proj, phase: "tech_specs",
                                              decision: .requestChanges, body: "Tighten the schema.")
        XCTAssertEqual(changes.lastPathComponent, "tech_specs.changes")
        XCTAssertEqual(try String(contentsOf: changes, encoding: .utf8), "Tighten the schema.")

        // Approve: an .ok file exists (body content is irrelevant to the engine).
        let ok = try ApprovalFiles.write(projectDir: proj, phase: "tech_specs",
                                         decision: .approve, body: "ok")
        XCTAssertTrue(FileManager.default.fileExists(atPath: ok.path))
        XCTAssertEqual(ok.deletingLastPathComponent().lastPathComponent, "approvals")
    }
}

// MARK: - blocked_conflict parsing (agent_state.json)

final class BlockedConflictTests: XCTestCase {

    private func data(_ s: String) -> Data { Data(s.utf8) }

    func testParsesLaneFilesDetail() {
        let state = """
        {"error": null, "done": false, "awaiting_approval": null,
         "blocked_conflict": {"lane": "codex-a", "files": ["App.swift", "Model.swift"],
                              "detail": "overlapping edits to App.swift"}}
        """
        let bc = BlockedConflict.parse(stateData: data(state))
        XCTAssertNotNil(bc)
        XCTAssertEqual(bc?.lane, "codex-a")
        XCTAssertEqual(bc?.files, ["App.swift", "Model.swift"])
        XCTAssertEqual(bc?.detail, "overlapping edits to App.swift")
        XCTAssertEqual(bc?.filesDisplay, "App.swift, Model.swift")
    }

    func testNullOrMissingIsNil() {
        XCTAssertNil(BlockedConflict.parse(stateData: data("{\"blocked_conflict\": null}")))
        XCTAssertNil(BlockedConflict.parse(stateData: data("{\"done\": true}")))
        XCTAssertNil(BlockedConflict.parse(stateData: data("not json at all")))
        // Wrong type (string instead of object) is treated as absent.
        XCTAssertNil(BlockedConflict.parse(stateData: data("{\"blocked_conflict\": \"boom\"}")))
    }

    func testDefensiveFieldFallbacks() {
        // files as a single string, missing lane/detail.
        let bc = BlockedConflict.parse(stateData: data(
            "{\"blocked_conflict\": {\"files\": \"App.swift\"}}"))
        XCTAssertEqual(bc?.lane, "?")
        XCTAssertEqual(bc?.files, ["App.swift"])
        XCTAssertEqual(bc?.detail, "")

        // Empty object still surfaces as a conflict (the run IS blocked).
        let empty = BlockedConflict.parse(stateData: data("{\"blocked_conflict\": {}}"))
        XCTAssertNotNil(empty)
        XCTAssertEqual(empty?.filesDisplay, "unknown files")

        // Non-string entries in files are skipped.
        let mixed = BlockedConflict.parse(stateData: data(
            "{\"blocked_conflict\": {\"files\": [\"A.swift\", 3, null]}}"))
        XCTAssertEqual(mixed?.files, ["A.swift"])
    }
}

// MARK: - Doctor local_models parsing (spec §12/§27)

final class DoctorReportParserTests: XCTestCase {

    private func data(_ s: String) -> Data { Data(s.utf8) }

    func testParsesFullLocalModelsBlock() {
        let json = """
        {"schema_version": 1, "tools": {"ollama": {"present": true}},
         "local_models": {
           "server_running": true, "selected": "qwen2.5-coder:7b",
           "selected_installed": true,
           "registry": [
             {"id": "qwen2.5-coder:7b", "label": "Fast Local Coding Assistant", "installed": true,
              "license": "Apache-2.0", "commercial_use": true, "min_ram_gb": 16,
              "recommended_ram_gb": 24, "size_gb": 4.7,
              "roles": ["implementation", "review"], "notes": "small coder"},
             {"id": "deepseek-r1:8b", "label": "Local Reasoning Reviewer", "installed": false},
             {"id": "mistral:7b", "label": "Small Local Reviewer", "installed": false}
           ]}}
        """
        let info = DoctorReportParser.localModels(fromDoctorJSON: data(json))
        XCTAssertNotNil(info)
        XCTAssertTrue(info?.serverRunning ?? false)
        XCTAssertEqual(info?.selected, "qwen2.5-coder:7b")
        XCTAssertTrue(info?.selectedInstalled ?? false)
        XCTAssertEqual(info?.registry.count, 3)
        XCTAssertEqual(info?.registry.first?.id, "qwen2.5-coder:7b")
        XCTAssertEqual(info?.registry.first?.label, "Fast Local Coding Assistant")
        XCTAssertTrue(info?.registry.first?.installed ?? false)
        XCTAssertEqual(info?.registry.first?.license, "Apache-2.0")
        XCTAssertTrue(info?.registry.first?.commercialUse ?? false)
        XCTAssertEqual(info?.registry.first?.minRAMGB, 16)
        XCTAssertEqual(info?.registry.first?.recommendedRAMGB, 24)
        XCTAssertEqual(info?.registry.first?.sizeGB, 4.7)
        XCTAssertEqual(info?.registry.first?.roles, ["implementation", "review"])
        XCTAssertEqual(info?.registry.first?.notes, "small coder")
        XCTAssertFalse(info?.registry.last?.installed ?? true)
    }

    func testMissingBlockOrMalformedJSONIsNil() {
        XCTAssertNil(DoctorReportParser.localModels(fromDoctorJSON: data("{\"tools\": {}}")))
        XCTAssertNil(DoctorReportParser.localModels(fromDoctorJSON: data("{not json")))
        XCTAssertNil(DoctorReportParser.localModels(fromDoctorJSON: Data()))
        XCTAssertNil(DoctorReportParser.localModels(fromDoctorJSON: data("[1, 2]")))
        // wrong type for the block is treated as absent
        XCTAssertNil(DoctorReportParser.localModels(fromDoctorJSON:
            data("{\"local_models\": \"nope\"}")))
    }

    func testDefensiveFieldFallbacks() {
        // Field-level problems default rather than dropping the block: entries
        // without an id are skipped, missing flags read as false/empty.
        let json = """
        {"local_models": {"registry": [
            {"label": "no id, skipped"}, "junk", 7,
            {"id": "mistral:7b"}
        ]}}
        """
        let info = DoctorReportParser.localModels(fromDoctorJSON: data(json))
        XCTAssertNotNil(info)
        XCTAssertFalse(info?.serverRunning ?? true)
        XCTAssertEqual(info?.selected, "")
        XCTAssertFalse(info?.selectedInstalled ?? true)
        XCTAssertEqual(info?.registry, [LocalModelEntry(id: "mistral:7b", label: "",
                                                        installed: false)])
    }

    func testRegistryIdShellSafety() {
        // Only registry ids ever reach a pull command (spec §12.3); a tampered
        // id with shell metacharacters must be refused.
        XCTAssertTrue(LocalModelEntry(id: "qwen2.5-coder:7b", label: "", installed: false)
            .idIsSafeForShell)
        XCTAssertTrue(LocalModelEntry(id: "library/llama3.1:8b-instruct_q4", label: "",
                                      installed: false).idIsSafeForShell)
        XCTAssertFalse(LocalModelEntry(id: "x; rm -rf ~", label: "", installed: false)
            .idIsSafeForShell)
        XCTAssertFalse(LocalModelEntry(id: "a\"b", label: "", installed: false).idIsSafeForShell)
        XCTAssertFalse(LocalModelEntry(id: "$(evil)", label: "", installed: false).idIsSafeForShell)
        XCTAssertFalse(LocalModelEntry(id: "", label: "", installed: false).idIsSafeForShell)
    }

    func testHardwareFitLabels() {
        let model = LocalModelEntry(id: "qwen3-coder:30b", minRAMGB: 32,
                                    recommendedRAMGB: 64)
        XCTAssertEqual(model.fitLabel(totalRAMGB: 128), "good fit")
        XCTAssertEqual(model.fitLabel(totalRAMGB: 32), "tight fit")
        XCTAssertEqual(model.fitLabel(totalRAMGB: 16), "too large")
        XCTAssertEqual(LocalModelEntry(id: "x").fitLabel(totalRAMGB: 64), "fit unknown")
    }
}

// MARK: - Roles file decoding

final class RolesFileJSONTests: XCTestCase {
    func testDecodesAgentRoleOverrides() throws {
        let json = """
        {"roles": [{"id": "qa", "name": "QA", "focus": "risk"}],
         "personalities": [{"id": "skeptic", "name": "the Skeptic", "style": "push back"}],
         "agent_role_overrides": {"codex": "qa", "ollama": "qa"}}
        """
        let decoded = try JSONDecoder().decode(RolesFileJSON.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.agentRoleOverrides?["codex"], "qa")
        XCTAssertEqual(decoded.agentRoleOverrides?["ollama"], "qa")
        XCTAssertEqual(decoded.roles?.first?.name, "QA")
    }
}

// MARK: - Shipped workflow coverage

final class WorkflowCoverageTests: XCTestCase {

    private func engineRoot() throws -> URL {
        let fm = FileManager.default
        var dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        for _ in 0..<8 {
            if fm.fileExists(atPath: dir.appendingPathComponent("workflows").path) {
                return dir
            }
            dir.deleteLastPathComponent()
        }
        throw NSError(domain: "WorkflowCoverageTests", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "Could not locate engine root"])
    }

    func testEveryShippedWorkflowDecodesForGUI() throws {
        let workflowsDir = try engineRoot().appendingPathComponent("workflows", isDirectory: true)
        let workflowURLs = try FileManager.default.contentsOfDirectory(
            at: workflowsDir,
            includingPropertiesForKeys: nil
        )
        .filter { $0.pathExtension == "json" }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }

        XCTAssertFalse(workflowURLs.isEmpty, "No shipped workflows found")

        var decodedNames: Set<String> = []
        for url in workflowURLs {
            let data = try Data(contentsOf: url)
            let json = try JSONDecoder().decode(WorkflowJSON.self, from: data)
            let def = WorkflowDef.from(json)

            decodedNames.insert(def.name)
            XCTAssertEqual(def.name, url.deletingPathExtension().lastPathComponent)
            XCTAssertFalse(def.title.isEmpty, "\(url.lastPathComponent) has no title")
            XCTAssertFalse(def.target.isEmpty, "\(url.lastPathComponent) has no target")
            XCTAssertFalse(def.phases.isEmpty, "\(url.lastPathComponent) has no phases")

            let phaseKeys = Set(def.phases.map(\.key))
            XCTAssertEqual(phaseKeys.count, def.phases.count,
                           "\(url.lastPathComponent) has duplicate phase keys")

            for phase in def.phases {
                XCTAssertFalse(phase.key.isEmpty, "\(url.lastPathComponent) has an empty phase key")
                XCTAssertFalse(phase.folder.isEmpty,
                               "\(url.lastPathComponent)/\(phase.key) has no folder")
                XCTAssertFalse(phase.file.isEmpty,
                               "\(url.lastPathComponent)/\(phase.key) has no output file")
                XCTAssertTrue(phase.file.hasSuffix(".md"),
                              "\(url.lastPathComponent)/\(phase.key) should write markdown")
                XCTAssertFalse(phase.title.isEmpty,
                               "\(url.lastPathComponent)/\(phase.key) has no title")
                XCTAssertGreaterThan(phase.rounds, 0,
                                     "\(url.lastPathComponent)/\(phase.key) has invalid rounds")
            }
        }

        let expectedBuiltIns: Set<String> = [
            "answer_question", "app_build", "audit", "iterate", "library_mining",
            "productionize", "research", "sprint", "vslice"
        ]
        XCTAssertTrue(expectedBuiltIns.isSubset(of: decodedNames))
    }
}

// MARK: - Engine-dir fallback precedence

final class EngineDirResolverTests: XCTestCase {

    private let bundled = URL(fileURLWithPath: "/Applications/Orchestrator.app/Contents/Resources/engine",
                              isDirectory: true)
    private let repoA = URL(fileURLWithPath: "/repo/gui", isDirectory: true)
    private let repoB = URL(fileURLWithPath: "/repo", isDirectory: true)

    func testBundledWinsOverRepo() {
        let choice = EngineDirResolver.pick(bundledTemplate: bundled,
                                            repoCandidates: [repoA, repoB],
                                            hasEngine: { _ in true })
        XCTAssertEqual(choice, .bundled(bundled))
    }

    func testFallsBackToFirstRepoCandidateWithEngine() {
        // Bundled template path exists as a URL but has no engine in it.
        let choice = EngineDirResolver.pick(bundledTemplate: bundled,
                                            repoCandidates: [repoA, repoB],
                                            hasEngine: { $0 == repoB })
        XCTAssertEqual(choice, .repo(repoB))
    }

    func testNearestRepoCandidateWinsWhenSeveralHaveEngine() {
        let choice = EngineDirResolver.pick(bundledTemplate: nil,
                                            repoCandidates: [repoA, repoB],
                                            hasEngine: { _ in true })
        XCTAssertEqual(choice, .repo(repoA))
    }

    func testMissingWhenNothingHasEngine() {
        XCTAssertEqual(EngineDirResolver.pick(bundledTemplate: bundled,
                                              repoCandidates: [repoA, repoB],
                                              hasEngine: { _ in false }), .missing)
        XCTAssertEqual(EngineDirResolver.pick(bundledTemplate: nil,
                                              repoCandidates: [],
                                              hasEngine: { _ in true }), .missing)
    }

    func testRepoLayoutCandidatesWalkAncestorsNearestFirst() {
        let exe = URL(fileURLWithPath:
            "/repo/gui/.build/arm64-apple-macosx/release/OrchestratorGUI")
        let candidates = EngineDirResolver.repoLayoutCandidates(executableURL: exe)
        let paths = candidates.map(\.path)
        XCTAssertEqual(paths.first, "/repo/gui/.build/arm64-apple-macosx/release")
        XCTAssertTrue(paths.contains("/repo/gui"))
        XCTAssertTrue(paths.contains("/repo"))
        // Nearest-first ordering: gui/ before the repo root.
        XCTAssertLessThan(paths.firstIndex(of: "/repo/gui")!, paths.firstIndex(of: "/repo")!)
        // Walking stops at the filesystem root instead of looping.
        XCTAssertEqual(EngineDirResolver.repoLayoutCandidates(executableURL: nil), [])
    }
}

// MARK: - Markdown rendering (transcript bubbles)

final class MarkdownRendererTests: XCTestCase {

    func testInlineStylesAreParsedNotLiteral() {
        let rendered = MarkdownRenderer.attributed("This is **bold** and `code`.")
        let plain = String(rendered.characters)
        XCTAssertFalse(plain.contains("**"), "bold markers must not render literally")
        XCTAssertFalse(plain.contains("`"), "code fences must not render literally")
        XCTAssertTrue(plain.contains("bold"))
        // The 'bold' run actually carries an inline intent (not stripped to plain).
        let hasStyledRun = rendered.runs.contains { $0.inlinePresentationIntent != nil }
        XCTAssertTrue(hasStyledRun)
    }

    func testNewlinesPreserved() {
        let rendered = MarkdownRenderer.attributed("line one\nline two")
        XCTAssertTrue(String(rendered.characters).contains("\n"))
    }

    func testPlainTextSurvivesUnchanged() {
        let body = "no markdown here, just text with 1 + 1 = 2"
        XCTAssertEqual(String(MarkdownRenderer.attributed(body).characters), body)
    }
}

// MARK: - Run-log tail buffer

final class RunLogBufferTests: XCTestCase {

    func testUnderCapUntouched() {
        let s = "short log\nwith lines\n"
        XCTAssertEqual(RunLogBuffer.trim(s, cap: 100, keep: 50), s)
    }

    func testTrimsOnLineBoundary() {
        // 30 numbered lines; cap forces a trim that would land mid-line.
        let log = (1...30).map { "line number \($0) padded out" }.joined(separator: "\n")
        let trimmed = RunLogBuffer.trim(log, cap: 200, keep: 150)
        XCTAssertLessThanOrEqual(trimmed.count, 150)
        // Starts on a whole line, not a partial one.
        XCTAssertTrue(trimmed.hasPrefix("line number "), "got: \(trimmed.prefix(30))")
    }

    func testDegenerateSingleLineStillBounded() {
        let log = String(repeating: "x", count: 500)   // no newline at all
        let trimmed = RunLogBuffer.trim(log, cap: 200, keep: 150)
        XCTAssertLessThanOrEqual(trimmed.count, 150)
        XCTAssertFalse(trimmed.isEmpty)
    }
}

// MARK: - Xcode project discovery (Open in Xcode)

final class XcodeProjectLocatorTests: XCTestCase {

    private func makeTempDir() throws -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("xcloc-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        return dir
    }

    func testFindsXcodeprojBundle() throws {
        let build = try makeTempDir()
        let proj = build.appendingPathComponent("MyApp.xcodeproj", isDirectory: true)
        try FileManager.default.createDirectory(at: proj, withIntermediateDirectories: true)
        XCTAssertEqual(XcodeProjectLocator.find(under: build)?.lastPathComponent,
                       "MyApp.xcodeproj")
    }

    func testIgnoresWorkspaceInsideXcodeproj() throws {
        let build = try makeTempDir()
        let inner = build.appendingPathComponent(
            "MyApp.xcodeproj/project.xcworkspace", isDirectory: true)
        try FileManager.default.createDirectory(at: inner, withIntermediateDirectories: true)
        // The project bundle itself is returned, not its internal workspace.
        XCTAssertEqual(XcodeProjectLocator.find(under: build)?.pathExtension, "xcodeproj")
    }

    func testMissingBuildDirIsNil() {
        let ghost = URL(fileURLWithPath: "/definitely/not/here-\(UUID().uuidString)")
        XCTAssertNil(XcodeProjectLocator.find(under: ghost))
    }

    func testNoProjectAnywhereIsNil() throws {
        let build = try makeTempDir()
        try Data().write(to: build.appendingPathComponent("main.swift"))
        XCTAssertNil(XcodeProjectLocator.find(under: build))
    }
}

// MARK: - Fenced code-block segmentation

final class MarkdownSegmentsTests: XCTestCase {

    func testProseOnlyIsOneSegment() {
        let segs = MarkdownRenderer.segments("just some prose\nover two lines")
        XCTAssertEqual(segs.count, 1)
        XCTAssertFalse(segs[0].isCode)
    }

    func testFenceSplitsProseAndCode() {
        let body = "before\n```swift\nlet x = 1\n```\nafter"
        let segs = MarkdownRenderer.segments(body)
        XCTAssertEqual(segs.map(\.isCode), [false, true, false])
        XCTAssertEqual(segs[1].text, "let x = 1")
        XCTAssertEqual(segs[0].text, "before")
        XCTAssertEqual(segs[2].text, "after")
    }

    func testUnclosedFenceRunsToEnd() {
        let segs = MarkdownRenderer.segments("intro\n```\ncode to the end")
        XCTAssertEqual(segs.map(\.isCode), [false, true])
        XCTAssertEqual(segs[1].text, "code to the end")
    }
}
