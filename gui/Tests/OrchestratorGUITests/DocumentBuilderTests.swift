import XCTest
@testable import OrchestratorGUI

final class DocumentBuilderRoundTripTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("document-builder-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: root) }

    private func fixture(_ slots: [String]) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema_version": 1, "name": "fixture", "description": "kept",
            "doc_slots": slots, "pipeline_ref": "future-pipeline",
            "overrides": ["sections": ["qa": ["enabled": false]],
                          "future_override": ["opaque": true]],
            "future_top": ["nested": [1, 2, 3]],
        ], options: [.prettyPrinted, .sortedKeys])
    }

    private func decoded(_ data: Data) throws -> SituationCanvas {
        switch SituationCodec.decode(data) {
        case .success(let value): return value
        case .failure(let error): throw NSError(domain: "test", code: 1,
                                                 userInfo: [NSLocalizedDescriptionKey: error])
        }
    }

    func testEmptyReorderedAndAllFortyFlowsRoundTripInExactOrder() throws {
        for slots in [[], ["c", "a", "b"], (0..<40).map { "slot-\($0)" }] {
            let input = try fixture(slots)
            let canvas = try decoded(input)
            XCTAssertEqual(canvas.slotIDs, slots)
            switch SituationCodec.encode(canvas) {
            case .success(let output): XCTAssertEqual(output, input, "untouched bytes must be exact")
            case .failure(let error): XCTFail(error)
            }
        }
    }

    func testReorderChangesOnlyDocSlotsAndPreservesUnknownNestedKeys() throws {
        let input = try fixture(["a", "b", "c"])
        var canvas = try decoded(input)
        canvas.slotIDs = ["c", "a", "b"]
        canvas.isDirty = true
        let output: Data
        switch SituationCodec.encode(canvas) {
        case .success(let value): output = value
        case .failure(let error): return XCTFail(error)
        }
        var before = try XCTUnwrap(JSONSerialization.jsonObject(with: input) as? [String: Any])
        var after = try XCTUnwrap(JSONSerialization.jsonObject(with: output) as? [String: Any])
        XCTAssertEqual(after["doc_slots"] as? [String], ["c", "a", "b"])
        before.removeValue(forKey: "doc_slots"); after.removeValue(forKey: "doc_slots")
        XCTAssertTrue(NSDictionary(dictionary: before).isEqual(to: after),
                      "no field except doc_slots may change semantically")
    }

    func testAtomicApplyAndExternalEditGuardPreserveBothSides() throws {
        let url = root.appendingPathComponent("situations/x/situation.json")
        let original = try fixture(["a"])
        var canvas = try decoded(original)
        canvas.slotIDs.append("b"); canvas.isDirty = true
        let saved = try SituationFileIO.save(canvas, to: url)
        XCTAssertEqual(try Data(contentsOf: url), saved)
        let external = try fixture(["external"])
        XCTAssertFalse(SituationFileIO.changedOnDisk(isDirty: false,
                                                      baseline: saved, current: external))
        XCTAssertTrue(SituationFileIO.changedOnDisk(isDirty: true,
                                                     baseline: saved, current: external))
        XCTAssertTrue(SituationFileIO.changedOnDisk(isDirty: true,
                                                     baseline: saved, current: nil),
                      "deletion is an external edit and must block Apply")
        XCTAssertEqual(try Data(contentsOf: url), saved,
                       "detecting an external edit never writes either version")
    }

    func testCorruptSituationRemainsOnDiskAndRepairCanvasCanEncode() throws {
        let url = root.appendingPathComponent("broken/situation.json")
        let corrupt = Data("not-json".utf8)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try corrupt.write(to: url)
        let records = SituationFileIO.load(root: root)
        XCTAssertEqual(records.first?.error, "situation.json is not a JSON object")
        XCTAssertEqual(try Data(contentsOf: url), corrupt)
        let repair = SituationCanvas(name: "broken", slotIDs: ["a"], rawRoot: [
            "name": .string("broken"), "doc_slots": .array([])
        ], originalData: nil, isDirty: true)
        switch SituationCodec.encode(repair) {
        case .success: break
        case .failure(let error): XCTFail(error)
        }
    }
}

final class DocumentBuilderImpactTests: XCTestCase {
    private let map = DocumentMap(
        categories: [DocumentCategory(id: "one", title: "One")],
        slots: [DocumentSlot(id: "a", title: "Alpha", category: "one", ownerSection: "research"),
                DocumentSlot(id: "b", title: "Beta", category: "one", ownerSection: "build"),
                DocumentSlot(id: "unowned", title: "Unowned", category: "one", ownerSection: nil)])

    func testImpactMatchesEngineFunctionsOnSameFixture() throws {
        let phases = [
            SituationWorkflowPhase(key: "research", title: "Research", docSections: ["a"]),
            SituationWorkflowPhase(key: "build", title: "Build", docSections: ["b"]),
            SituationWorkflowPhase(key: "design", title: "Design", docSections: ["design"]),
            SituationWorkflowPhase(key: "final", title: "Final", docSections: []),
        ]
        let swift = SituationImpactCompiler.preview(slotIDs: ["b", "a", "a", "stale"],
                                                    map: map, phases: phases)
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let script = """
import json,sys
sys.path.insert(0, sys.argv[1])
import situations, completeness, workflows
dm={'slots':[{'slot_id':'a','owner_section':'research'},{'slot_id':'b','owner_section':'build'},{'slot_id':'unowned','owner_section':None}]}
s={'doc_slots':['b','a','a','stale']}
slots,owners=situations.resolve_required_slots(s,dm)
ph=[workflows.Phase('research','r','r.md','',doc_sections=['a']),workflows.Phase('build','b','b.md','',doc_sections=['b']),workflows.Phase('design','d','d.md','',doc_sections=['design']),workflows.Phase('final','f','f.md','',doc_sections=[])]
kept=completeness.filter_phases_by_slots(ph,slots)
print(json.dumps({'sections':sorted(owners),'phase_count':len(kept)}))
"""
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", script, repo.path]
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = Pipe()
        try process.run(); process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let engine = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(swift.sections, engine["sections"] as? [String])
        XCTAssertEqual(swift.phaseCount, engine["phase_count"] as? Int)
    }

    func testPreviewParityForEveryEngineSeededSituation() throws {
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let mapData = try Data(contentsOf: repo.appendingPathComponent("sections/documentation/doc_map.json"))
        let realMap: DocumentMap
        switch DocumentMapCodec.decode(mapData) {
        case .success(let value): realMap = value
        case .failure(let error): return XCTFail(error)
        }
        let workflowData = try Data(contentsOf: repo.appendingPathComponent("workflows/app_build.json"))
        let workflow = try XCTUnwrap(JSONSerialization.jsonObject(with: workflowData) as? [String: Any])
        let swiftPhases = ((workflow["phases"] as? [[String: Any]]) ?? []).enumerated().map { index, raw in
            SituationWorkflowPhase(key: (raw["key"] as? String) ?? "p\(index)",
                                   title: (raw["title"] as? String) ?? "p\(index)",
                                   docSections: (raw["doc_sections"] as? [String]) ?? [])
        }
        let script = """
import json,sys
sys.path.insert(0,sys.argv[1])
import situations,completeness,workflows
dm=json.load(open(sys.argv[1]+'/sections/documentation/doc_map.json'))
ph=workflows.load_workflow('app_build',sys.argv[1]).phases
out=[]
for name,body in sorted(situations._SEEDS.items()):
 s={'doc_slots':body['doc_slots']}
 slots,owners=situations.resolve_required_slots(s,dm)
 kept=completeness.filter_phases_by_slots(ph,slots)
 out.append({'name':name,'slots':body['doc_slots'],'sections':sorted(owners),'phase_count':len(kept)})
print(json.dumps(out))
"""
        let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", script, repo.path]
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = Pipe()
        try process.run(); process.waitUntilExit(); XCTAssertEqual(process.terminationStatus, 0)
        let rows = try XCTUnwrap(JSONSerialization.jsonObject(
            with: pipe.fileHandleForReading.readDataToEndOfFile()) as? [[String: Any]])
        XCTAssertEqual(rows.count, 6)
        for row in rows {
            let slots = try XCTUnwrap(row["slots"] as? [String])
            let preview = SituationImpactCompiler.preview(slotIDs: slots, map: realMap,
                                                          phases: swiftPhases)
            XCTAssertEqual(preview.sections, row["sections"] as? [String], row["name"] as? String ?? "")
            XCTAssertEqual(preview.phaseCount, row["phase_count"] as? Int, row["name"] as? String ?? "")
        }
    }

    func testBrokenNarrowFilterFallsBackToAllAndEmptySlotsAreNoOp() {
        let phases = [SituationWorkflowPhase(key: "a", title: "A", docSections: []),
                      SituationWorkflowPhase(key: "final", title: "Final", docSections: [])]
        XCTAssertEqual(SituationImpactCompiler.preview(slotIDs: ["a"], map: map,
                                                       phases: phases).phaseCount, 2)
        XCTAssertEqual(SituationImpactCompiler.preview(slotIDs: [], map: map,
                                                       phases: phases).phaseCount, 2)
    }

    func testOwnedUnownedAndGapStatesAreExplicit() {
        XCTAssertEqual(map.slotsByID["a"]?.ownerSection, "research")
        XCTAssertNil(map.slotsByID["unowned"]?.ownerSection,
                     "an unowned chip must remain unowned rather than inventing a section")
        let report = """
# App — Gap Report
## Coverage
- Filled: 0 / 3
- Thin (under min_chars): 1
- Empty: 1
- Lineage conflicts: 1
## Open gaps
- **Alpha** (research) — thin; 3/200 chars; evidence: x
- **Beta** (build) — empty; 0/200 chars; evidence: none
- **Unowned** (None) — lineage_conflict; 0/200 chars; evidence: a,b
"""
        let parsed = GapReportParser.parse(report, map: map)
        XCTAssertNil(parsed.error)
        XCTAssertEqual(parsed.statuses, ["a": .thin, "b": .empty, "unowned": .conflict])
        XCTAssertEqual(DocumentFillStatus.neutral.rawValue, "neutral",
                       "no-project state is distinct from engine-computed empty")
        XCTAssertNotNil(GapReportParser.parse("corrupt", map: map).error,
                        "corrupt reports never masquerade as empty slot data")
    }

    func testRealBlueprintHasElevenCategoriesAndFortyOwnedChips() throws {
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: repo.appendingPathComponent("sections/documentation/doc_map.json"))
        switch DocumentMapCodec.decode(data) {
        case .failure(let error): XCTFail(error)
        case .success(let decoded):
            XCTAssertEqual(decoded.categories.count, 11)
            XCTAssertEqual(decoded.slots.count, 40)
            XCTAssertTrue(decoded.slots.allSatisfy { $0.ownerSection != nil })
        }
    }
}
