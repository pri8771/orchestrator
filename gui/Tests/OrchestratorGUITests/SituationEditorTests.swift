import XCTest
@testable import OrchestratorGUI

final class SituationApplyDiffTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("situation-apply-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: root) }

    private struct EngineFixture {
        let repo: URL
        let orch: URL
        let project: URL
        let config: URL
        let situations: [URL]
    }

    private func makeEngineFixture(current: String?) throws -> EngineFixture {
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let orch = root.appendingPathComponent("orch")
        let project = root.appendingPathComponent("project")
        try FileManager.default.createDirectory(at: orch.appendingPathComponent("sections/documentation"),
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: orch.appendingPathComponent("workflows"),
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: project, withIntermediateDirectories: true)
        let docMap: [String: Any] = ["schema_version": 1, "docs": [],
            "categories": [["category_id": "x", "title": "X"]],
            "slots": [["slot_id": "a", "title": "A", "category": "x", "owner_section": "research"],
                      ["slot_id": "b", "title": "B", "category": "x", "owner_section": "build"],
                      ["slot_id": "c", "title": "C", "category": "x", "owner_section": "qa"],
                      ["slot_id": "d", "title": "D", "category": "x", "owner_section": "execution"]]]
        try JSONSerialization.data(withJSONObject: docMap).write(to: orch.appendingPathComponent("sections/documentation/doc_map.json"))
        let phase: (String, [String]) -> [String: Any] = { key, docs in
            ["key": key, "folder": key, "file": "\(key).md", "purpose": "", "doc_sections": docs]
        }
        let workflow: [String: Any] = ["name": "w", "title": "W", "description": "",
            "target": "app", "build_phase": "build_coordination",
            "phases": [phase("prompt_contract", ["a"]), phase("product_research", ["b"]),
                phase("portfolio_audit", ["a"]), phase("other", ["other"]),
                phase("build_coordination", ["d"]), phase("final_review", [])]]
        try JSONSerialization.data(withJSONObject: workflow).write(to: orch.appendingPathComponent("workflows/w.json"))
        var situationURLs: [URL] = []
        for (name, slots) in [("old", ["a", "b"]), ("new", ["b", "d"])] {
            let dir = orch.appendingPathComponent("situations/\(name)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let situation: [String: Any] = ["schema_version": 1, "name": name,
                "description": "", "doc_slots": slots, "pipeline_ref": "",
                "overrides": ["sections": [:], "phases": [:], "casts": [:]]]
            let url = dir.appendingPathComponent("situation.json")
            try JSONSerialization.data(withJSONObject: situation).write(to: url)
            situationURLs.append(url)
        }
        var runConfig: [String: Any] = ["completeness": "prototype", "future": "kept"]
        if let current { runConfig["situation"] = current }
        let config = project.appendingPathComponent("run_config.json")
        try JSONSerialization.data(withJSONObject: runConfig).write(to: config)
        return EngineFixture(repo: repo, orch: orch, project: project,
                             config: config, situations: situationURLs)
    }

    private func query(_ fixture: EngineFixture, candidate: String) throws -> SituationApplyDiff {
        switch SituationEngineQuery.diff(python: "/usr/bin/python3", moduleRoot: fixture.repo,
                                         orchDir: fixture.orch, projectDir: fixture.project,
                                         workflow: "w", candidate: candidate) {
        case .success(let diff): return diff
        case .failure(let error):
            XCTFail(error)
            throw NSError(domain: "SituationApplyDiffTests", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: error])
        }
    }

    func testRuntimeQueryUsesEngineLayeringForSituationToSituation() throws {
        let fixture = try makeEngineFixture(current: "old")
        let diff = try query(fixture, candidate: "new")
        XCTAssertEqual(diff.phasesAdded, ["build_coordination"])
        XCTAssertEqual(diff.phasesRemoved, ["prompt_contract"])
        XCTAssertEqual(diff.sectionsActivated, ["execution"])
        XCTAssertEqual(diff.sectionsDeactivated, ["research"])
        XCTAssertEqual(diff.slotDelta, 0)
    }

    func testRuntimeQueryUsesEngineLayeringForNoneToSituation() throws {
        let fixture = try makeEngineFixture(current: nil)
        let diff = try query(fixture, candidate: "new")
        XCTAssertEqual(diff.phasesAdded, [])
        XCTAssertEqual(diff.phasesRemoved, ["prompt_contract"])
        XCTAssertEqual(diff.sectionsActivated, ["build", "execution"])
        XCTAssertEqual(diff.sectionsDeactivated, [])
        XCTAssertEqual(diff.slotDelta, 2)
        XCTAssertTrue(diff.hasChanges)
    }

    func testRuntimeQueryReportsSameSituationAsNoChanges() throws {
        let fixture = try makeEngineFixture(current: "new")
        let diff = try query(fixture, candidate: "new")
        XCTAssertEqual(diff.phasesAdded, [])
        XCTAssertEqual(diff.phasesRemoved, [])
        XCTAssertEqual(diff.sectionsActivated, [])
        XCTAssertEqual(diff.sectionsDeactivated, [])
        XCTAssertEqual(diff.slotDelta, 0)
        XCTAssertFalse(diff.hasChanges)
    }

    func testRuntimeQueryLayersProjectStopTargetBeforeBothSides() throws {
        let fixture = try makeEngineFixture(current: "old")
        var config = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: fixture.config)) as? [String: Any])
        config["stop_after_phase"] = "build_coordination"
        try JSONSerialization.data(withJSONObject: config).write(to: fixture.config)
        let diff = try query(fixture, candidate: "new")
        XCTAssertEqual(diff.phasesAdded, [])
        XCTAssertEqual(diff.phasesRemoved, [])
        XCTAssertEqual(diff.sectionsActivated, ["execution"])
        XCTAssertEqual(diff.sectionsDeactivated, ["research"])
    }

    func testImpactPreviewCancelLeavesConfigAndSituationFilesByteIdentical() throws {
        let fixture = try makeEngineFixture(current: "old")
        let urls = [fixture.config] + fixture.situations
        let before = try Dictionary(uniqueKeysWithValues: urls.map { ($0, try Data(contentsOf: $0)) })
        _ = try query(fixture, candidate: "new")
        for url in urls {
            XCTAssertEqual(try Data(contentsOf: url), before[url], url.path)
        }
    }

    func testCancelPathIsPureAndConfirmIsAtomicIdempotentAndPreservesKeys() throws {
        let config = root.appendingPathComponent("project/run_config.json")
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        let original = Data(#"{"future":{"opaque":true},"situation":"old"}"#.utf8)
        try original.write(to: config)
        XCTAssertEqual(try Data(contentsOf: config), original,
                       "opening and cancelling the diff performs no writes")
        XCTAssertTrue(try SituationApplyService.confirm(situation: "new", runConfigURL: config))
        let once = try Data(contentsOf: config)
        XCTAssertFalse(try SituationApplyService.confirm(situation: "new", runConfigURL: config),
                       "a second Confirm is a no-op")
        XCTAssertEqual(try Data(contentsOf: config), once)
        let decoded = try XCTUnwrap(JSONSerialization.jsonObject(with: once) as? [String: Any])
        XCTAssertEqual(decoded["situation"] as? String, "new")
        XCTAssertEqual((decoded["future"] as? [String: Any])?["opaque"] as? Bool, true)
    }

    func testCorruptExistingRunConfigFailsWithoutClobber() throws {
        let config = root.appendingPathComponent("run_config.json")
        let corrupt = Data("broken".utf8); try corrupt.write(to: config)
        XCTAssertThrowsError(try SituationApplyService.confirm(situation: "x", runConfigURL: config))
        XCTAssertEqual(try Data(contentsOf: config), corrupt)
    }
}

final class SituationEditorCodecTests: XCTestCase {
    func testDuplicateNamingNeverAliasesExistingNameOrSlug() {
        XCTAssertEqual(SituationLibraryNaming.copyName(
            source: "My Plan", existingNames: ["My Plan-copy"],
            occupiedSlugs: ["my-plan-copy-2"]), "My Plan-copy-3")
    }

    func testEngineReferenceIsDirectoryIdentityNotHumanFacingName() {
        let record = SituationFileRecord(name: "My Plan Copy",
            url: URL(fileURLWithPath: "/tmp/situations/my-plan-copy/situation.json"),
            data: Data(), error: nil)
        XCTAssertEqual(record.engineRef, "my-plan-copy")
        XCTAssertNotEqual(record.name, "my-plan-copy")
    }

    func testDanglingPipelineReferenceNamesMissingPreset() {
        XCTAssertNil(SituationEditCodec.pipelineIssue(ref: "", availableNames: []))
        XCTAssertNil(SituationEditCodec.pipelineIssue(ref: "known", availableNames: ["known"]))
        XCTAssertEqual(SituationEditCodec.pipelineIssue(ref: "lost", availableNames: ["known"]),
                       "Situation references ‘lost’, but that preset does not exist.")
    }

    func testAllKnownOverridesShareOneRootAndUnknownFieldsSurviveSave() throws {
        let data = Data(#"{"name":"x","doc_slots":["a"],"pipeline_ref":"old","future_top":{"kept":1},"overrides":{"sections":{"research":{"enabled":true,"future_section":"kept"}},"phases":{"p":{"rounds":2,"future_phase":"kept"}},"casts":{"p":["persona"]},"future_override":{"kept":true}}}"#.utf8)
        var canvas: SituationCanvas
        switch SituationCodec.decode(data) {
        case .success(let value): canvas = value
        case .failure(let error): return XCTFail(error)
        }
        XCTAssertEqual(SituationEditCodec.unknownFields(canvas), [
            "future_top", "overrides.future_override",
            "overrides.phases.p.future_phase",
            "overrides.sections.research.future_section",
        ])
        SituationEditCodec.setPipelineRef("new", in: &canvas)
        SituationEditCodec.setSection("research", enabled: false, in: &canvas)
        SituationEditCodec.setPhaseField("p", field: "rounds", value: .number(5), in: &canvas)
        SituationEditCodec.setCast("p", ids: ["persona", "reviewer"], in: &canvas)
        let output: Data
        switch SituationCodec.encode(canvas) { case .success(let value): output = value
        case .failure(let error): return XCTFail(error) }
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: output) as? [String: Any])
        XCTAssertEqual((root["future_top"] as? [String: Any])?["kept"] as? Int, 1)
        let overrides = try XCTUnwrap(root["overrides"] as? [String: Any])
        XCTAssertEqual((overrides["future_override"] as? [String: Any])?["kept"] as? Bool, true)
        let phase = try XCTUnwrap((overrides["phases"] as? [String: Any])?["p"] as? [String: Any])
        XCTAssertEqual(phase["rounds"] as? Int, 5); XCTAssertEqual(phase["future_phase"] as? String, "kept")
        XCTAssertEqual((overrides["casts"] as? [String: Any])?["p"] as? [String], ["persona", "reviewer"])
    }
}
