import XCTest
@testable import OrchestratorGUI

final class PipelineRoundTripTests: XCTestCase {
    private var root: URL!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("pipeline-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root,
                                                withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func fixture(nodeCount: Int = 3) throws -> (Data, Set<String>) {
        let sections = (0..<nodeCount).map { "s\($0)" }
        let rules: [[String: Any]] = (0..<nodeCount).map { index in
            ["match": ["artifact_type": "type\(index)",
                       "source_section": sections[index]],
             "strategy": index == nodeCount - 1 ? "chain" : "one",
             "targets": [sections[(index + 1) % nodeCount]],
             "hop_budget": index + 2,
             "rule_id": "edge-\(index)",
             "future_edge": ["kept": index]]
        }
        let object: [String: Any] = [
            "schema_version": 1,
            "preset_name": "Ten node cycle",
            "routing": ["artifact_routes": ["legacy": "s0"],
                        "rules": rules,
                        "future_routing": "kept"],
            "goal_manifest": ["goal": ["doc_gap_empty": true],
                              "future_manifest": "kept",
                              "quiescence_cycles": 4,
                              "budgets": ["turns": 12, "wall_clock_s": 60]],
            "seed": ["section": "s0", "prompt_template": "Seed {{idea}}",
                     "future_seed": 9],
            "ui": ["nodes": sections.enumerated().map { index, section in
                ["id": section, "x": index * 10, "y": index * 20,
                 "future_node": "kept"] as [String: Any]
            }, "future_layout": ["zoom": 2]],
            "future_top": ["opaque": true],
        ]
        return (try JSONSerialization.data(withJSONObject: object,
                                            options: [.prettyPrinted, .sortedKeys]),
                Set(sections))
    }

    func testUntouchedJSONRoundTripIsByteExactIncludingUnknownKeys() throws {
        let (data, sections) = try fixture(nodeCount: 10)
        let canvas: PipelineCanvas
        switch PipelineCodec.decode(data, knownSections: sections) {
        case .success(let value): canvas = value
        case .failure(let error): return XCTFail(error)
        }
        XCTAssertEqual(canvas.nodes.count, 10)
        XCTAssertEqual(canvas.edges.count, 10)
        XCTAssertEqual(canvas.edges.last?.target, "s0", "cyclic feedback edge survives")
        switch PipelineCodec.encode(canvas) {
        case .success(let output): XCTAssertEqual(output, data)
        case .failure(let error): XCTFail(error)
        }
    }

    func testCanvasCompileWritesSourceSectionAndPreservesOpaqueFieldsAfterEdit() throws {
        let (data, sections) = try fixture()
        var canvas: PipelineCanvas
        switch PipelineCodec.decode(data, knownSections: sections) {
        case .success(let value): canvas = value
        case .failure(let error): return XCTFail(error)
        }
        canvas.name = "Edited cycle"
        canvas.nodes[0].x = 333
        canvas.edges[0].artifactType = "revised"
        canvas.isDirty = true
        let output: Data
        switch PipelineCodec.encode(canvas) {
        case .success(let data): output = data
        case .failure(let error): return XCTFail(error)
        }
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: output)
                                 as? [String: Any])
        XCTAssertEqual((root["future_top"] as? [String: Any])?["opaque"] as? Bool, true)
        let routing = try XCTUnwrap(root["routing"] as? [String: Any])
        XCTAssertEqual(routing["future_routing"] as? String, "kept")
        let rules = try XCTUnwrap(routing["rules"] as? [[String: Any]])
        let match = try XCTUnwrap(rules[0]["match"] as? [String: Any])
        XCTAssertEqual(match["artifact_type"] as? String, "revised")
        XCTAssertEqual(match["source_section"] as? String, "s0",
                       "a drawn edge must never compile as a wildcard route")
        XCTAssertEqual((rules[0]["future_edge"] as? [String: Any])?["kept"] as? Int, 0)
        let ui = try XCTUnwrap(root["ui"] as? [String: Any])
        XCTAssertEqual((ui["future_layout"] as? [String: Any])?["zoom"] as? Int, 2)
        let nodes = try XCTUnwrap(ui["nodes"] as? [[String: Any]])
        XCTAssertEqual(nodes[0]["x"] as? Int, 333)
        XCTAssertEqual(nodes[0]["future_node"] as? String, "kept")

        switch PipelineCodec.decode(output, knownSections: sections) {
        case .success(let decoded):
            XCTAssertEqual(decoded.name, canvas.name)
            XCTAssertEqual(decoded.nodes.map(\.section), canvas.nodes.map(\.section))
            XCTAssertEqual(decoded.edges.map(\.source), canvas.edges.map(\.source))
            XCTAssertEqual(decoded.edges.map(\.target), canvas.edges.map(\.target))
            XCTAssertEqual(decoded.edges.map(\.artifactType), canvas.edges.map(\.artifactType))
            XCTAssertEqual(decoded.edges.map(\.strategy), canvas.edges.map(\.strategy))
            XCTAssertEqual(decoded.edges.map(\.hopBudget), canvas.edges.map(\.hopBudget))
        case .failure(let error): XCTFail(error)
        }
    }

    func testInvalidPresetNamesSpecificFieldAndKeepsFileEditableOnDisk() throws {
        let data = Data(#"{"preset_name":"Broken","routing":{"rules":[{"match":{"artifact_type":"idea"},"targets":["research"]}]},"goal_manifest":{},"seed":{"section":"ideas","prompt_template":"x"}}"#.utf8)
        let url = root.appendingPathComponent("broken.json")
        try data.write(to: url)
        switch PipelineCodec.decode(data, knownSections: ["ideas", "research"]) {
        case .success: XCTFail("wildcard edge must not render as a drawn edge")
        case .failure(let error):
            XCTAssertTrue(error.contains("routing.rules[0].match.source_section"))
        }
        XCTAssertEqual(try Data(contentsOf: url), data,
                       "failed validation preserves the user's file byte-for-byte")
    }

    func testEveryRuleWithMultipleTargetsDecompilesToVisibleEdges() throws {
        let object: [String: Any] = [
            "preset_name": "Fan out",
            "routing": ["artifact_routes": [:], "rules": [[
                "match": ["artifact_type": "idea", "source_section": "ideas"],
                "strategy": "every", "targets": ["research", "planning"],
                "rule_id": "fanout"
            ]]],
            "goal_manifest": [:],
            "seed": ["section": "ideas", "prompt_template": "{{idea}}"],
            "ui": ["nodes": []],
        ]
        let data = try JSONSerialization.data(withJSONObject: object)
        switch PipelineCodec.decode(
            data, knownSections: ["ideas", "research", "planning"]) {
        case .failure(let error): XCTFail(error)
        case .success(let canvas):
            XCTAssertEqual(canvas.edges.count, 2)
            XCTAssertEqual(Set(canvas.edges.map(\.target)), ["research", "planning"])
            XCTAssertEqual(Set(canvas.edges.map(\.strategy)), ["every"])
            XCTAssertEqual(Set(canvas.edges.map(\.source)), ["ideas"])
        }
    }

    func testCompilerRefusesBrokenEdgeWithSpecificIndex() throws {
        var canvas = PipelineCanvas(
            name: "Broken", nodes: [
                PipelineNode(section: "ideas", x: 0, y: 0),
                PipelineNode(section: "research", x: 100, y: 0),
            ], edges: [PipelineEdge(id: "bad", source: "ideas",
                                    target: "research", artifactType: "")],
            seedSection: "ideas", promptTemplate: "{{idea}}", isDirty: true)
        canvas.rawRoot["future"] = .string("preserved while user repairs")
        switch PipelineCodec.encode(canvas) {
        case .success: XCTFail("invalid work must not be saved or run")
        case .failure(let error):
            XCTAssertTrue(error.contains("routing.rules[0].match.artifact_type"))
        }
        XCTAssertEqual(canvas.rawRoot["future"]?.string,
                       "preserved while user repairs")
    }

    func testSeedThenDiskWinsCorruptionIsVisibleAndSameNamesNeverOverwrite() throws {
        let dir = root.appendingPathComponent("presets")
        let first = PipelinePresetLibrary.load(
            dir: dir, knownSections: ["ideas", "research", "planning",
                                      "documentation", "execution"])
        XCTAssertEqual(first.records.count, 3)
        XCTAssertNotNil(first.warning)
        let custom = Data(#"{"preset_name":"Same","routing":{"artifact_routes":{},"rules":[]},"goal_manifest":{},"seed":{"section":"ideas","prompt_template":"{{idea}}"},"ui":{}}"#.utf8)
        let a = try PipelinePresetLibrary.save(custom, name: "Same", dir: dir)
        let b = try PipelinePresetLibrary.save(custom, name: "Same", dir: dir)
        XCTAssertNotEqual(a, b)
        XCTAssertEqual(try Data(contentsOf: a), custom)
        try Data("not-json".utf8).write(to: dir.appendingPathComponent("corrupt.json"))
        let next = PipelinePresetLibrary.load(
            dir: dir, knownSections: ["ideas", "research", "planning",
                                      "documentation", "execution"])
        XCTAssertNil(next.warning, "existing disk presets win; seeds are not reapplied")
        XCTAssertTrue(next.records.contains { $0.url.lastPathComponent == "corrupt.json"
            && $0.error?.contains("invalid JSON") == true })
    }

    func testRunFilesMintOneDeterministicSeedAndWriteOnlyRealHandoffContracts() throws {
        let canvas = PipelineCanvas(
            name: "Brainstorm to Plan",
            nodes: [PipelineNode(section: "ideas", x: 0, y: 0)], edges: [],
            seedSection: "ideas", promptTemplate: "Brief: {{idea}}",
            isDirty: true)
        let first = try PipelineRunFiles.seed(
            root: root, project: "Gloam", canvas: canvas,
            idea: "Offline notes", workflow: "brainstorm")
        let second = try PipelineRunFiles.seed(
            root: root, project: "Gloam", canvas: canvas,
            idea: "Offline notes", workflow: "brainstorm")
        let distinct = try PipelineRunFiles.seed(
            root: root, project: "Gloam", canvas: canvas,
            idea: "A genuinely different seed", workflow: "brainstorm")
        XCTAssertTrue(first.newlyMinted)
        XCTAssertFalse(second.newlyMinted)
        XCTAssertEqual(first.sessionID, second.sessionID)
        XCTAssertNotEqual(first.sessionID, distinct.sessionID,
                          "different seed text must never alias the first run")
        XCTAssertEqual(try String(contentsOf: root.appendingPathComponent(
            "\(first.sessionID)/initial_prompt/initial_prompt.md"), encoding: .utf8),
                       "Brief: Offline notes\n")
        XCTAssertEqual(try String(contentsOf: root.appendingPathComponent(
            "\(first.sessionID)/workflow.txt"), encoding: .utf8), "brainstorm\n")
        let sectionEntries = try FileManager.default.contentsOfDirectory(atPath:
            root.appendingPathComponent("gloam/ideas").path)
        XCTAssertEqual(sectionEntries.filter { !$0.hasPrefix(".") }.count, 2,
                       "same-seed double invocation mints one; a different seed mints another")

        let preset = root.appendingPathComponent("preset.json")
        try Data("{}".utf8).write(to: preset)
        try PipelineRunFiles.writeRequest(root: root, presetURL: preset)
        let request = try XCTUnwrap(JSONSerialization.jsonObject(with:
            Data(contentsOf: root.appendingPathComponent(
                ".conductor/pipeline_request.json"))) as? [String: Any])
        XCTAssertEqual(request["preset_path"] as? String, preset.path)
    }

    func testPipelineSeedRequiresAnAddressableNamedSectionWorkflow() throws {
        let named = Data(#"{"workflow":"brainstorm"}"#.utf8)
        let inline = Data(#"{"workflow":{"name":"design_studio","phases":[]}}"#.utf8)
        XCTAssertEqual(PipelineRunFiles.namedWorkflow(fromSectionManifest: named),
                       "brainstorm")
        XCTAssertNil(PipelineRunFiles.namedWorkflow(fromSectionManifest: inline),
                     "an inline display name is not a resolvable workflow.txt contract")
        XCTAssertNil(PipelineRunFiles.namedWorkflow(
            fromSectionManifest: Data("not-json".utf8)))
    }

    func testExistingConductorMustPositivelyProveRouteMode() throws {
        let dir = root.appendingPathComponent(".conductor")
        try FileManager.default.createDirectory(at: dir,
                                                withIntermediateDirectories: true)
        try "pid=4242 host=test\n".write(to: dir.appendingPathComponent(
            "conductor.lock"), atomically: true, encoding: .utf8)
        XCTAssertTrue(PipelineRunFiles.runningConductorHasRouting(
            root: root, commandForPID: { pid in
                XCTAssertEqual(pid, 4242)
                return "/usr/bin/python3 /repo/conductor.py --root /tmp/w --route"
            }))
        XCTAssertFalse(PipelineRunFiles.runningConductorHasRouting(
            root: root, commandForPID: { _ in
                "/usr/bin/python3 /repo/conductor.py --root /tmp/w"
            }), "observation-only mode must never be presented as runnable")
        XCTAssertFalse(PipelineRunFiles.runningConductorHasRouting(
            root: root, commandForPID: { _ in nil }),
            "an unverifiable process fails closed")
    }
}
