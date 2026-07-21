import XCTest
@testable import OrchestratorGUI

final class SituationApplyDiffTests: XCTestCase {
    private var root: URL!
    private let map = DocumentMap(
        categories: [DocumentCategory(id: "x", title: "X")],
        slots: [DocumentSlot(id: "a", title: "A", category: "x", ownerSection: "research"),
                DocumentSlot(id: "b", title: "B", category: "x", ownerSection: "build"),
                DocumentSlot(id: "c", title: "C", category: "x", ownerSection: "qa")])
    private let phases = [
        SituationWorkflowPhase(key: "research", title: "Research", docSections: ["a"]),
        SituationWorkflowPhase(key: "build", title: "Build", docSections: ["b"]),
        SituationWorkflowPhase(key: "qa", title: "QA", docSections: ["c"]),
        SituationWorkflowPhase(key: "other", title: "Other", docSections: ["other"]),
        SituationWorkflowPhase(key: "final", title: "Final", docSections: []),
    ]

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("situation-apply-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: root) }

    func testNoneToSituationSituationToSituationAndSameToSame() {
        let fromNone = SituationApplyService.diff(beforeSlots: [], afterSlots: ["a", "b"],
                                                  map: map, phases: phases)
        XCTAssertEqual(fromNone.phasesAdded, [])
        XCTAssertEqual(fromNone.phasesRemoved, ["qa", "other"])
        XCTAssertEqual(fromNone.sectionsActivated, ["build", "research"])
        XCTAssertEqual(fromNone.slotDelta, 2)

        let changed = SituationApplyService.diff(beforeSlots: ["a", "b"], afterSlots: ["b", "c"],
                                                 map: map, phases: phases)
        XCTAssertEqual(changed.phasesAdded, ["qa"])
        XCTAssertEqual(changed.phasesRemoved, ["research"])
        XCTAssertEqual(changed.sectionsActivated, ["qa"])
        XCTAssertEqual(changed.sectionsDeactivated, ["research"])

        let same = SituationApplyService.diff(beforeSlots: ["b", "c"], afterSlots: ["b", "c"],
                                              map: map, phases: phases)
        XCTAssertFalse(same.hasChanges)
        XCTAssertEqual(same.slotDelta, 0)
    }

    func testDiffPhaseAndSectionSetsMatchEnginePath() throws {
        let swift = SituationApplyService.diff(beforeSlots: ["a", "b"], afterSlots: ["b", "c"],
                                               map: map, phases: phases)
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let script = """
import json,sys
sys.path.insert(0,sys.argv[1])
import situations,completeness,workflows
dm={'slots':[{'slot_id':'a','owner_section':'research'},{'slot_id':'b','owner_section':'build'},{'slot_id':'c','owner_section':'qa'}]}
ph=[workflows.Phase('research','r','r.md','',doc_sections=['a']),workflows.Phase('build','b','b.md','',doc_sections=['b']),workflows.Phase('qa','q','q.md','',doc_sections=['c']),workflows.Phase('other','o','o.md','',doc_sections=['other']),workflows.Phase('final','f','f.md','',doc_sections=[])]
def resolve(slots):
 req,owners=situations.resolve_required_slots({'doc_slots':slots},dm)
 return [p.key for p in completeness.filter_phases_by_slots(ph,req)],sorted(owners)
b,bo=resolve(['a','b']); a,ao=resolve(['b','c'])
print(json.dumps({'added':[x for x in a if x not in b],'removed':[x for x in b if x not in a],'activated':[x for x in ao if x not in bo],'deactivated':[x for x in bo if x not in ao]}))
"""
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", script, repo.path]
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = Pipe()
        try process.run(); process.waitUntilExit(); XCTAssertEqual(process.terminationStatus, 0)
        let engine = try XCTUnwrap(JSONSerialization.jsonObject(
            with: pipe.fileHandleForReading.readDataToEndOfFile()) as? [String: Any])
        XCTAssertEqual(swift.phasesAdded, engine["added"] as? [String])
        XCTAssertEqual(swift.phasesRemoved, engine["removed"] as? [String])
        XCTAssertEqual(swift.sectionsActivated, engine["activated"] as? [String])
        XCTAssertEqual(swift.sectionsDeactivated, engine["deactivated"] as? [String])
    }

    func testRuntimeQueryUsesEngineLayeringAndReturnsNamedDiff() throws {
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
        for (name, slots) in [("old", ["a", "b"]), ("new", ["b", "d"])] {
            let dir = orch.appendingPathComponent("situations/\(name)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let situation: [String: Any] = ["schema_version": 1, "name": name,
                "description": "", "doc_slots": slots, "pipeline_ref": "",
                "overrides": ["sections": [:], "phases": [:], "casts": [:]]]
            try JSONSerialization.data(withJSONObject: situation).write(to: dir.appendingPathComponent("situation.json"))
        }
        try Data(#"{"situation":"old","completeness":"prototype","future":"kept"}"#.utf8)
            .write(to: project.appendingPathComponent("run_config.json"))
        switch SituationEngineQuery.diff(python: "/usr/bin/python3", moduleRoot: repo,
                                         orchDir: orch, projectDir: project,
                                         workflow: "w", candidate: "new") {
        case .failure(let error): XCTFail(error)
        case .success(let diff):
            XCTAssertEqual(diff.phasesAdded, ["build_coordination"])
            XCTAssertEqual(diff.phasesRemoved, ["prompt_contract"])
            XCTAssertEqual(diff.sectionsActivated, ["execution"])
            XCTAssertEqual(diff.sectionsDeactivated, ["research"])
            XCTAssertEqual(diff.slotDelta, 0)
        }
    }

    func testCancelPathIsPureAndConfirmIsAtomicIdempotentAndPreservesKeys() throws {
        let config = root.appendingPathComponent("project/run_config.json")
        try FileManager.default.createDirectory(at: config.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        let original = Data(#"{"future":{"opaque":true},"situation":"old"}"#.utf8)
        try original.write(to: config)
        _ = SituationApplyService.diff(beforeSlots: ["a"], afterSlots: ["b"],
                                       map: map, phases: phases)
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
        XCTAssertEqual(SituationEditCodec.unknownPhaseFields(canvas), ["p.future_phase"])
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
