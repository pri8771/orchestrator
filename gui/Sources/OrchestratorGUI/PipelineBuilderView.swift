import Foundation
import SwiftUI

// V3 7.11: a pipeline is serialized Conductor configuration, not a second
// scheduler. Raw dictionaries ride every modeled node/edge so edits preserve
// fields introduced by newer engines. An untouched decode returns its exact
// original bytes; after an edit, sorted JSON provides deterministic output.

indirect enum PipelineJSON: Equatable, Sendable {
    case object([String: PipelineJSON])
    case array([PipelineJSON])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init?(_ value: Any) {
        if value is NSNull { self = .null; return }
        if let value = value as? Bool { self = .bool(value); return }
        if let value = value as? String { self = .string(value); return }
        if let value = value as? NSNumber { self = .number(value.doubleValue); return }
        if let value = value as? [Any] {
            var out: [PipelineJSON] = []
            for item in value { guard let parsed = PipelineJSON(item) else { return nil }; out.append(parsed) }
            self = .array(out); return
        }
        if let value = value as? [String: Any] {
            var out: [String: PipelineJSON] = [:]
            for (key, item) in value { guard let parsed = PipelineJSON(item) else { return nil }; out[key] = parsed }
            self = .object(out); return
        }
        return nil
    }

    var any: Any {
        switch self {
        case .object(let value): return value.mapValues(\.any)
        case .array(let value): return value.map(\.any)
        case .string(let value): return value
        case .number(let value): return NSNumber(value: value)
        case .bool(let value): return value
        case .null: return NSNull()
        }
    }

    var object: [String: PipelineJSON]? { if case .object(let value) = self { return value }; return nil }
    var array: [PipelineJSON]? { if case .array(let value) = self { return value }; return nil }
    var string: String? { if case .string(let value) = self { return value }; return nil }
    var double: Double? { if case .number(let value) = self { return value }; return nil }
    var int: Int? { double.map(Int.init) }
    var bool: Bool? { if case .bool(let value) = self { return value }; return nil }
}

struct PipelineNode: Identifiable, Equatable, Sendable {
    var section: String
    var x: Double
    var y: Double
    var raw: [String: PipelineJSON] = [:]
    var id: String { section }
}

struct PipelineEdge: Identifiable, Equatable, Sendable {
    var id: String
    var source: String
    var target: String
    var artifactType: String
    var strategy: String = "one"
    var hopBudget: Int = 4
    var raw: [String: PipelineJSON] = [:]
}

struct PipelineCanvas: Equatable, Sendable {
    var name: String
    var nodes: [PipelineNode]
    var edges: [PipelineEdge]
    var seedSection: String
    var promptTemplate: String
    var docGapEmpty = false
    var quiescenceCycles: Int?
    var turnsCap: Int?
    var wallClockCap: Double?
    var rawRoot: [String: PipelineJSON] = [:]
    var originalData: Data?
    var isDirty = false
}

enum PipelineResult<Value> {
    case success(Value)
    case failure(String)
}

enum PipelineCodec {
    static func decode(_ data: Data, knownSections: Set<String>) -> PipelineResult<PipelineCanvas> {
        guard let any = try? JSONSerialization.jsonObject(with: data),
              let root = PipelineJSON(any)?.object else {
            return .failure("preset: invalid JSON object")
        }
        guard let name = root["preset_name"]?.string,
              !name.trimmingCharacters(in: .whitespaces).isEmpty else {
            return .failure("preset_name: missing or empty")
        }
        guard let routing = root["routing"]?.object else {
            return .failure("routing: missing or not an object")
        }
        guard let goal = root["goal_manifest"]?.object else {
            return .failure("goal_manifest: missing or not an object")
        }
        if let cycles = goal["quiescence_cycles"],
           cycles.int.map({ $0 > 0 }) != true {
            return .failure("goal_manifest.quiescence_cycles: must be a positive int")
        }
        if let checks = goal["goal"]?.object {
            let allowed = Set(["doc_gap_empty", "dod_tier", "eval_threshold"])
            if let unknown = checks.keys.first(where: { !allowed.contains($0) }) {
                return .failure("goal_manifest.goal.\(unknown): unknown goal check")
            }
            if let value = checks["doc_gap_empty"], value.bool == nil {
                return .failure("goal_manifest.goal.doc_gap_empty: must be a bool")
            }
            if let value = checks["dod_tier"], value.string == nil {
                return .failure("goal_manifest.goal.dod_tier: must be a string")
            }
            if let value = checks["eval_threshold"], value.double == nil {
                return .failure("goal_manifest.goal.eval_threshold: must be numeric")
            }
        }
        guard let seed = root["seed"]?.object,
              let seedSection = seed["section"]?.string, !seedSection.isEmpty else {
            return .failure("seed.section: missing or empty")
        }
        guard knownSections.contains(seedSection) else {
            return .failure("seed.section: unknown section '\(seedSection)'")
        }
        guard let prompt = seed["prompt_template"]?.string else {
            return .failure("seed.prompt_template: missing or not a string")
        }

        var edges: [PipelineEdge] = []
        for (index, item) in (routing["rules"]?.array ?? []).enumerated() {
            guard let raw = item.object else {
                return .failure("routing.rules[\(index)]: must be an object")
            }
            let match = raw["match"]?.object ?? [:]
            let artifact = match["artifact_type"]?.string
                ?? raw["artifact_type"]?.string ?? ""
            let source = match["source_section"]?.string
                ?? raw["source_section"]?.string ?? ""
            let targets = raw["targets"]?.array?.compactMap(\.string)
                ?? raw["target"]?.string.map { [$0] } ?? []
            guard !artifact.isEmpty else {
                return .failure("routing.rules[\(index)].match.artifact_type: missing")
            }
            guard !source.isEmpty else {
                return .failure("routing.rules[\(index)].match.source_section: missing (canvas edges cannot be wildcard routes)")
            }
            guard !targets.isEmpty else {
                return .failure("routing.rules[\(index)].targets: missing")
            }
            guard knownSections.contains(source) else {
                return .failure("routing.rules[\(index)]: unknown source section '\(source)'")
            }
            let strategy = raw["strategy"]?.string ?? "one"
            guard ["one", "every", "chain"].contains(strategy) else {
                return .failure("routing.rules[\(index)].strategy: unknown '\(strategy)'")
            }
            if strategy != "every" && targets.count > 1 {
                return .failure("routing.rules[\(index)].targets: \(strategy) requires one target")
            }
            for (targetIndex, target) in targets.enumerated() {
                guard knownSections.contains(target) else {
                    return .failure("routing.rules[\(index)]: unknown target section '\(target)'")
                }
                let ruleID = raw["rule_id"]?.string ?? "edge-\(index)"
                edges.append(PipelineEdge(
                    id: targets.count == 1 ? ruleID : "\(ruleID):\(targetIndex):\(target)",
                    source: source, target: target, artifactType: artifact,
                    strategy: strategy,
                    hopBudget: max(1, raw["hop_budget"]?.int ?? 4), raw: raw))
            }
        }

        let ui = root["ui"]?.object ?? [:]
        var nodes = (ui["nodes"]?.array ?? []).compactMap { value -> PipelineNode? in
            guard let raw = value.object,
                  let section = raw["id"]?.string ?? raw["section"]?.string,
                  knownSections.contains(section) else { return nil }
            return PipelineNode(section: section, x: raw["x"]?.double ?? 80,
                                y: raw["y"]?.double ?? 80, raw: raw)
        }
        let referenced = Set(edges.flatMap { [$0.source, $0.target] } + [seedSection])
        for section in referenced.sorted() where !nodes.contains(where: { $0.section == section }) {
            let index = nodes.count
            nodes.append(PipelineNode(section: section,
                                      x: Double(60 + (index % 4) * 190),
                                      y: Double(60 + (index / 4) * 120)))
        }
        let goalChecks = goal["goal"]?.object ?? [:]
        let budgets = goal["budgets"]?.object ?? [:]
        return .success(PipelineCanvas(
            name: name, nodes: nodes, edges: edges, seedSection: seedSection,
            promptTemplate: prompt,
            docGapEmpty: goalChecks["doc_gap_empty"]?.bool ?? false,
            quiescenceCycles: goal["quiescence_cycles"]?.int,
            turnsCap: budgets["turns"]?.int,
            wallClockCap: budgets["wall_clock_s"]?.double,
            rawRoot: root, originalData: data, isDirty: false))
    }

    static func encode(_ canvas: PipelineCanvas) -> PipelineResult<Data> {
        if !canvas.isDirty, let original = canvas.originalData { return .success(original) }
        guard !canvas.name.trimmingCharacters(in: .whitespaces).isEmpty else {
            return .failure("preset_name: missing or empty")
        }
        let sections = Set(canvas.nodes.map(\.section))
        guard sections.contains(canvas.seedSection) else {
            return .failure("seed.section: add '\(canvas.seedSection)' to the canvas")
        }
        var rules: [PipelineJSON] = []
        for (index, edge) in canvas.edges.enumerated() {
            guard sections.contains(edge.source), sections.contains(edge.target) else {
                return .failure("routing.rules[\(index)]: source and target nodes must exist")
            }
            guard !edge.artifactType.trimmingCharacters(in: .whitespaces).isEmpty else {
                return .failure("routing.rules[\(index)].match.artifact_type: missing")
            }
            guard ["one", "every", "chain"].contains(edge.strategy) else {
                return .failure("routing.rules[\(index)].strategy: unknown '\(edge.strategy)'")
            }
            var raw = edge.raw
            var match = raw["match"]?.object ?? [:]
            match["artifact_type"] = .string(edge.artifactType)
            match["source_section"] = .string(edge.source)
            raw["match"] = .object(match)
            raw.removeValue(forKey: "artifact_type")
            raw.removeValue(forKey: "source_section")
            raw["strategy"] = .string(edge.strategy)
            raw["targets"] = .array([.string(edge.target)])
            raw["hop_budget"] = .number(Double(max(1, edge.hopBudget)))
            raw["rule_id"] = .string(edge.id)
            rules.append(.object(raw))
        }

        var root = canvas.rawRoot
        root["schema_version"] = .number(1)
        root["preset_name"] = .string(canvas.name.trimmingCharacters(in: .whitespaces))
        var routing = root["routing"]?.object ?? [:]
        routing["artifact_routes"] = routing["artifact_routes"] ?? .object([:])
        routing["rules"] = .array(rules)
        root["routing"] = .object(routing)
        var goal = root["goal_manifest"]?.object ?? [:]
        var checks = goal["goal"]?.object ?? [:]
        if canvas.docGapEmpty { checks["doc_gap_empty"] = .bool(true) }
        else { checks.removeValue(forKey: "doc_gap_empty") }
        goal["goal"] = .object(checks)
        if let value = canvas.quiescenceCycles { goal["quiescence_cycles"] = .number(Double(max(1, value))) }
        else { goal.removeValue(forKey: "quiescence_cycles") }
        var budgets = goal["budgets"]?.object ?? [:]
        if let value = canvas.turnsCap { budgets["turns"] = .number(Double(max(1, value))) }
        else { budgets.removeValue(forKey: "turns") }
        if let value = canvas.wallClockCap { budgets["wall_clock_s"] = .number(max(0, value)) }
        else { budgets.removeValue(forKey: "wall_clock_s") }
        if budgets.isEmpty { goal.removeValue(forKey: "budgets") }
        else { goal["budgets"] = .object(budgets) }
        root["goal_manifest"] = .object(goal)
        var seed = root["seed"]?.object ?? [:]
        seed["section"] = .string(canvas.seedSection)
        seed["prompt_template"] = .string(canvas.promptTemplate)
        root["seed"] = .object(seed)
        var ui = root["ui"]?.object ?? [:]
        ui["nodes"] = .array(canvas.nodes.map { node in
            var raw = node.raw
            raw["id"] = .string(node.section)
            raw["x"] = .number(node.x)
            raw["y"] = .number(node.y)
            return .object(raw)
        })
        root["ui"] = .object(ui)
        do {
            return .success(try JSONSerialization.data(
                withJSONObject: PipelineJSON.object(root).any,
                options: [.prettyPrinted, .sortedKeys]))
        } catch { return .failure("preset: couldn't encode JSON (\(error.localizedDescription))") }
    }
}

struct PipelinePresetRecord: Identifiable, Equatable {
    let name: String
    let url: URL
    let data: Data
    let error: String?
    var id: String { url.path }
}

enum PipelinePresetLibrary {
    static func load(dir: URL, knownSections: Set<String>) -> (records: [PipelinePresetRecord], warning: String?) {
        let fm = FileManager.default
        var warning: String?
        if ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? []).filter({ $0.hasSuffix(".json") }).isEmpty {
            do {
                try fm.createDirectory(at: dir, withIntermediateDirectories: true)
                for (name, data) in starterData() {
                    try data.write(to: dir.appendingPathComponent(name), options: .atomic)
                }
                warning = "Starter pipeline presets were seeded; files on disk now win."
            } catch {
                warning = "Starter preset fallback failed: \(error.localizedDescription)"
            }
        }
        let files = ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? [])
            .filter { $0.hasSuffix(".json") }.sorted()
        return (files.compactMap { filename in
            let url = dir.appendingPathComponent(filename)
            guard let data = try? Data(contentsOf: url) else { return nil }
            switch PipelineCodec.decode(data, knownSections: knownSections) {
            case .success(let canvas):
                return PipelinePresetRecord(name: canvas.name, url: url,
                                            data: data, error: nil)
            case .failure(let error):
                return PipelinePresetRecord(
                    name: filename.replacingOccurrences(of: ".json", with: ""),
                    url: url, data: data, error: error)
            }
        }, warning)
    }

    static func save(_ data: Data, name: String, dir: URL,
                     replacing: URL? = nil) throws -> URL {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let replacing { try data.write(to: replacing, options: .atomic); return replacing }
        let base = OrchestratorStore.slugify(name)
        var url = dir.appendingPathComponent(base + ".json")
        var n = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = dir.appendingPathComponent("\(base)-\(n).json"); n += 1
        }
        try data.write(to: url, options: .atomic)
        return url
    }

    static func starterData() -> [(String, Data)] {
        let specs: [(String, String, [(String, String, String)])] = [
            ("Brainstorm to Plan", "ideas", [("ideas", "research", "idea"),
                                               ("research", "planning", "research_brief")]),
            ("Research to Documentation", "research", [("research", "documentation", "research_brief")]),
            ("Plan to Execution", "planning", [("planning", "execution", "spec_bundle")]),
        ]
        return specs.compactMap { name, seed, edges in
            let nodes = Array(Set(edges.flatMap { [$0.0, $0.1] } + [seed])).sorted()
            let obj: [String: Any] = [
                "schema_version": 1, "preset_name": name,
                "routing": ["artifact_routes": [:], "rules": edges.enumerated().map { index, edge in
                    ["match": ["artifact_type": edge.2, "source_section": edge.0],
                     "strategy": "one", "targets": [edge.1], "hop_budget": 4,
                     "rule_id": "starter-\(index)-\(edge.0)-\(edge.1)"] as [String: Any]
                }],
                "goal_manifest": ["goal": ["doc_gap_empty": true],
                                  "quiescence_cycles": 3,
                                  "budgets": ["turns": 12, "wall_clock_s": 14400]],
                "seed": ["section": seed, "prompt_template": "{{idea}}"],
                "ui": ["nodes": nodes.enumerated().map { index, section in
                    ["id": section, "x": 60 + index * 200, "y": 90]
                }]
            ]
            guard let data = try? JSONSerialization.data(withJSONObject: obj,
                                                          options: [.prettyPrinted, .sortedKeys]) else { return nil }
            return (OrchestratorStore.slugify(name) + ".json", data)
        }
    }
}

struct PipelineSeedResult: Equatable {
    let sessionID: String
    let newlyMinted: Bool
}

enum PipelineRunFiles {
    static func runningConductorHasRouting(
        root: URL, commandForPID: (Int32) -> String? = processCommand
    ) -> Bool {
        let lock = root.appendingPathComponent(".conductor/conductor.lock")
        guard let text = try? String(contentsOf: lock, encoding: .utf8),
              let token = text.split(whereSeparator: { $0.isWhitespace })
                .first(where: { $0.hasPrefix("pid=") }),
              let pid = Int32(token.dropFirst(4)), pid > 0,
              let command = commandForPID(pid) else { return false }
        let words = command.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        return words.contains { $0.hasSuffix("conductor.py") }
            && words.contains("--route")
    }

    private static func processCommand(_ pid: Int32) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-p", String(pid), "-o", "command="]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do { try process.run(); process.waitUntilExit() }
        catch { return nil }
        guard process.terminationStatus == 0 else { return nil }
        return String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                      encoding: .utf8)
    }

    static func namedWorkflow(fromSectionManifest data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let workflow = object["workflow"] as? String,
              !workflow.isEmpty else { return nil }
        return workflow
    }

    static func render(template: String, idea: String) -> String {
        template.contains("{{idea}}")
            ? template.replacingOccurrences(of: "{{idea}}", with: idea)
            : template + "\n\n" + idea
    }

    static func seed(root: URL, project: String, canvas: PipelineCanvas,
                     idea: String, workflow: String) throws -> PipelineSeedResult {
        let fm = FileManager.default
        let projectSlug = OrchestratorStore.slugify(project)
        let hash = stableHash("\(canvas.name)\u{0}\(idea)")
        let sessionID = "\(projectSlug)/\(canvas.seedSection)/pipeline-\(hash)"
        let destination = root.appendingPathComponent(sessionID)
        if fm.fileExists(atPath: destination.path) {
            return PipelineSeedResult(sessionID: sessionID, newlyMinted: false)
        }
        let wrapper = root.appendingPathComponent(projectSlug)
        try fm.createDirectory(at: wrapper, withIntermediateDirectories: true)
        fm.createFile(atPath: wrapper.appendingPathComponent(
            SessionLayout.sectionsMarker).path, contents: Data())
        let sectionDir = wrapper.appendingPathComponent(canvas.seedSection)
        try fm.createDirectory(at: sectionDir, withIntermediateDirectories: true)
        let temp = sectionDir.appendingPathComponent(".pipeline-\(UUID().uuidString)")
        let promptDir = temp.appendingPathComponent("initial_prompt")
        do {
            try fm.createDirectory(at: promptDir, withIntermediateDirectories: true)
            try (render(template: canvas.promptTemplate, idea: idea) + "\n")
                .write(to: promptDir.appendingPathComponent("initial_prompt.md"),
                       atomically: true, encoding: .utf8)
            try (workflow + "\n").write(to: temp.appendingPathComponent("workflow.txt"),
                                          atomically: true, encoding: .utf8)
            do { try fm.moveItem(at: temp, to: destination) }
            catch where fm.fileExists(atPath: destination.path) {
                try? fm.removeItem(at: temp)
                return PipelineSeedResult(sessionID: sessionID, newlyMinted: false)
            }
            return PipelineSeedResult(sessionID: sessionID, newlyMinted: true)
        } catch {
            try? fm.removeItem(at: temp)
            throw error
        }
    }

    static func writeRequest(root: URL, presetURL: URL) throws {
        let dir = root.appendingPathComponent(".conductor")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let data = try JSONSerialization.data(
            withJSONObject: ["preset_path": presetURL.path], options: [.sortedKeys])
        try data.write(to: dir.appendingPathComponent("pipeline_request.json"),
                       options: .atomic)
    }

    private static func stableHash(_ text: String) -> String {
        var value: UInt64 = 1469598103934665603
        for byte in text.utf8 { value ^= UInt64(byte); value &*= 1099511628211 }
        return String(value, radix: 16)
    }
}

struct PipelineBuilderSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    @State private var records: [PipelinePresetRecord] = []
    @State private var selectedID: String?
    @State private var canvas: PipelineCanvas?
    @State private var selectedEdgeID: String?
    @State private var loadError: String?
    @State private var note: String?
    @State private var showRun = false
    @State private var runProject = ""
    @State private var runIdea = ""
    var initialPresetName: String? = nil
    var compact = false

    private var sections: [String] { store.knownPipelineSections().sorted() }
    private var selectedRecord: PipelinePresetRecord? {
        records.first { $0.id == selectedID }
    }

    var body: some View {
        Group {
            if compact {
                editor
            } else {
                VStack(spacing: 0) {
                    HStack {
                        Text("Pipelines").font(DS.font.headline)
                        Text("Cross-section Conductor presets")
                            .font(DS.font.caption).foregroundStyle(DS.textSecondary)
                        Spacer()
                        Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
                    }
                    .padding(.horizontal, DS.space.m).frame(height: 44)
                    Divider()
                    HSplitView {
                        presetList.frame(minWidth: 210, idealWidth: 240)
                        editor.frame(minWidth: 520)
                    }
                }
            }
        }
        .frame(minWidth: 780, minHeight: 560)
        .background(DS.windowBg)
        .onAppear { reload() }
        .sheet(isPresented: $showRun) { runSheet }
    }

    private var presetList: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack {
                Text("Presets").font(DS.font.headline)
                Spacer()
                Button { createPreset() } label: { Image(systemName: "plus") }
                    .help("New pipeline preset")
            }
            if records.isEmpty {
                EmptyStateView(symbol: "point.3.connected.trianglepath.dotted",
                               title: "No pipeline presets",
                               message: sections.isEmpty
                                ? "No seeded sections are installed."
                                : "Create a preset, add section nodes, then connect them.")
            } else {
                ScrollView {
                    VStack(spacing: DS.space.xxs) {
                        ForEach(records) { record in
                            Button { select(record) } label: {
                                HStack {
                                    Image(systemName: presetIcon(record))
                                    VStack(alignment: .leading) {
                                        Text(record.name).lineLimit(1)
                                        if let error = record.error {
                                            Text(error).lineLimit(2)
                                                .font(DS.font.caption2)
                                                .foregroundStyle(DS.status.error.color)
                                        }
                                    }
                                    Spacer()
                                }
                                .padding(DS.space.xs)
                                .background(selectedID == record.id
                                    ? AnyShapeStyle(DS.accent.fill) : DS.raised)
                                .clipShape(RoundedRectangle(cornerRadius: DS.radius.control))
                            }.buttonStyle(.plain)
                        }
                    }
                }
            }
            if let note { Text(note).font(DS.font.caption).foregroundStyle(DS.textSecondary) }
        }.padding(DS.space.s)
    }

    @ViewBuilder private var editor: some View {
        if let error = loadError {
            VStack(alignment: .leading, spacing: DS.space.m) {
                InlineBanner(kind: .error, title: "Preset can't be rendered",
                             message: error)
                Text("The JSON file was left untouched. Repair it on disk or delete it to the Trash.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
                if let record = selectedRecord {
                    Button("Move broken preset to Trash", role: .destructive) {
                        store.deletePipelinePreset(record); reload()
                    }
                }
                Spacer()
            }.padding(DS.space.m)
        } else if canvas != nil {
            pipelineEditor
        } else {
            EmptyStateView(symbol: "point.3.connected.trianglepath.dotted",
                           title: "Select a pipeline",
                           message: "The canvas is the preset file: nodes are sections and edges are Conductor rules.")
        }
    }

    private var pipelineEditor: some View {
        VStack(spacing: 0) {
            editorToolbar
            Divider()
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 0) {
                    canvasView
                    Divider()
                    inspector.frame(width: 280)
                }
                VStack(spacing: 0) {
                    canvasView.frame(minHeight: 320)
                    Divider()
                    inspector
                }
            }
        }
    }

    private var editorToolbar: some View {
        HStack(spacing: DS.space.xs) {
            TextField("Preset name", text: binding(\.name))
                .textFieldStyle(.roundedBorder).frame(maxWidth: 280)
            Menu("Add section") {
                ForEach(sections.filter { section in
                    !(canvas?.nodes.contains { $0.section == section } ?? false)
                }, id: \.self) { section in
                    Button(section.capitalized) { addNode(section) }
                }
            }
            Menu("Remove section") {
                ForEach(canvas?.nodes ?? []) { node in
                    Button(node.section.capitalized, role: .destructive) {
                        removeNode(node.section)
                    }
                }
            }.disabled((canvas?.nodes.count ?? 0) <= 1)
            Button("Add edge") { addEdge() }
                .disabled((canvas?.nodes.count ?? 0) < 2)
            Spacer()
            if livePresetIsOlder {
                Label("Live run uses older saved rules", systemImage: "clock.arrow.circlepath")
                    .font(DS.font.caption).foregroundStyle(DS.status.warning.color)
            }
            Button("Save") { save() }.keyboardShortcut("s", modifiers: .command)
            Button("Run pipeline") { showRun = true }.buttonStyle(.borderedProminent)
        }.padding(DS.space.s)
    }

    private var canvasView: some View {
        GeometryReader { proxy in
            ZStack(alignment: .topLeading) {
                Rectangle().fill(DS.insetBg)
                if let canvas {
                    ForEach(canvas.edges) { edge in
                        if let a = canvas.nodes.first(where: { $0.section == edge.source }),
                           let b = canvas.nodes.first(where: { $0.section == edge.target }) {
                            Path { path in
                                path.move(to: CGPoint(x: a.x + 70, y: a.y + 22))
                                path.addLine(to: CGPoint(x: b.x + 70, y: b.y + 22))
                            }.stroke(selectedEdgeID == edge.id
                                     ? DS.accent.color : DS.hairline,
                                     style: StrokeStyle(lineWidth: selectedEdgeID == edge.id ? 3 : 2,
                                                        lineCap: .round))
                        }
                    }
                    ForEach(canvas.nodes) { node in
                        VStack(spacing: DS.space.xxs) {
                            Text(node.section.capitalized).font(DS.font.headline)
                            Text("section").font(DS.font.caption2)
                                .foregroundStyle(DS.textSecondary)
                        }
                        .frame(width: 140, height: 44)
                        .background(DS.raised)
                        .clipShape(RoundedRectangle(cornerRadius: DS.radius.card))
                        .overlay(RoundedRectangle(cornerRadius: DS.radius.card)
                            .stroke(canvas.seedSection == node.section
                                    ? DS.accent.stroke : DS.hairline))
                        .position(x: min(max(75, node.x + 70), proxy.size.width - 75),
                                  y: min(max(24, node.y + 22), proxy.size.height - 24))
                        .gesture(DragGesture().onChanged { value in
                            moveNode(node.section,
                                     x: min(max(5, value.location.x - 70), proxy.size.width - 145),
                                     y: min(max(5, value.location.y - 22), proxy.size.height - 49))
                        })
                    }
                    ForEach(canvas.edges) { edge in
                        if let a = canvas.nodes.first(where: { $0.section == edge.source }),
                           let b = canvas.nodes.first(where: { $0.section == edge.target }) {
                            Button(edge.artifactType.isEmpty ? "condition" : edge.artifactType) {
                                selectedEdgeID = edge.id
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .position(x: (a.x + b.x) / 2 + 70,
                                      y: (a.y + b.y) / 2 + 22)
                        }
                    }
                }
            }.clipped()
        }
        .frame(minHeight: 420)
    }

    private var inspector: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.m) {
                Text("Pipeline settings").font(DS.font.headline)
                if wildcardRouteCount > 0 {
                    InlineBanner(kind: .warning, title: "Wildcard routes preserved",
                                 message: "\(wildcardRouteCount) type-wide artifact route(s) have no source section, so they remain in JSON but are not drawn as specific edges.")
                }
                Picker("Seed section", selection: binding(\.seedSection)) {
                    ForEach(canvas?.nodes ?? []) { Text($0.section.capitalized).tag($0.section) }
                }
                TextField("Prompt template (use {{idea}})", text: binding(\.promptTemplate), axis: .vertical)
                    .lineLimit(3...6)
                Toggle("Goal: documentation gap empty", isOn: binding(\.docGapEmpty))
                optionalInt("Quiescence cycles", keyPath: \.quiescenceCycles)
                optionalInt("Turn cap", keyPath: \.turnsCap)
                if let index = selectedEdgeIndex {
                    Divider()
                    Text("Selected edge").font(DS.font.headline)
                    Picker("From", selection: edgeBinding(index, \.source)) {
                        ForEach(canvas?.nodes ?? []) { Text($0.section).tag($0.section) }
                    }
                    Picker("To", selection: edgeBinding(index, \.target)) {
                        ForEach(canvas?.nodes ?? []) { Text($0.section).tag($0.section) }
                    }
                    TextField("Artifact type", text: edgeBinding(index, \.artifactType))
                    Picker("Mode", selection: edgeBinding(index, \.strategy)) {
                        Text("One").tag("one"); Text("Every").tag("every"); Text("Chain").tag("chain")
                    }
                    Stepper("Hop budget \(canvas?.edges[index].hopBudget ?? 4)",
                            value: edgeBinding(index, \.hopBudget), in: 1...32)
                    Button("Remove edge", role: .destructive) { removeEdge(index) }
                } else {
                    Text("Select an edge label to edit its condition and mode.")
                        .font(DS.font.caption).foregroundStyle(DS.textSecondary)
                }
            }.padding(DS.space.m)
        }
    }

    private var selectedEdgeIndex: Int? {
        guard let selectedEdgeID else { return nil }
        return canvas?.edges.firstIndex { $0.id == selectedEdgeID }
    }

    private var wildcardRouteCount: Int {
        canvas?.rawRoot["routing"]?.object?["artifact_routes"]?.object?.count ?? 0
    }

    private var livePresetIsOlder: Bool {
        guard let record = selectedRecord,
              store.missionControl.activePipelineName == canvas?.name,
              let loaded = store.missionControl.activePipelineLoadedAt,
              let attrs = try? FileManager.default.attributesOfItem(atPath: record.url.path),
              let modified = attrs[.modificationDate] as? Date else { return false }
        return modified > loaded
    }

    private func binding<Value>(_ keyPath: WritableKeyPath<PipelineCanvas, Value>) -> Binding<Value> {
        Binding(get: { canvas![keyPath: keyPath] }, set: { value in
            canvas![keyPath: keyPath] = value; canvas!.isDirty = true
        })
    }

    private func edgeBinding<Value>(_ index: Int,
                                    _ keyPath: WritableKeyPath<PipelineEdge, Value>) -> Binding<Value> {
        Binding(get: { canvas!.edges[index][keyPath: keyPath] }, set: { value in
            canvas!.edges[index][keyPath: keyPath] = value; canvas!.isDirty = true
        })
    }

    private func optionalInt(_ label: String,
                             keyPath: WritableKeyPath<PipelineCanvas, Int?>) -> some View {
        HStack {
            Toggle(label, isOn: Binding(get: { canvas![keyPath: keyPath] != nil }, set: { enabled in
                canvas![keyPath: keyPath] = enabled ? 3 : nil; canvas!.isDirty = true
            }))
            if canvas![keyPath: keyPath] != nil {
                Stepper("\(canvas![keyPath: keyPath]!)", value: Binding(
                    get: { canvas![keyPath: keyPath]! }, set: { value in
                        canvas![keyPath: keyPath] = value; canvas!.isDirty = true
                    }), in: 1...1000)
            }
        }
    }

    private func reload(selecting url: URL? = nil) {
        records = store.listPipelinePresets()
        if let warning = store.pipelinePresetWarning { note = warning }
        let target = url.flatMap { wanted in records.first { $0.url == wanted } }
            ?? initialPresetName.flatMap { wanted in records.first { $0.name == wanted } }
            ?? selectedRecord ?? records.first
        if let target { select(target) }
        else { selectedID = nil; canvas = nil; loadError = nil }
    }

    private func presetIcon(_ record: PipelinePresetRecord) -> String {
        record.error == nil
            ? "point.3.connected.trianglepath.dotted"
            : "exclamationmark.triangle"
    }

    private func select(_ record: PipelinePresetRecord) {
        selectedID = record.id
        selectedEdgeID = nil
        switch PipelineCodec.decode(record.data, knownSections: Set(sections)) {
        case .success(let decoded): canvas = decoded; loadError = nil
        case .failure(let error): canvas = nil; loadError = error
        }
    }

    private func createPreset() {
        guard let first = sections.first else { note = "No sections are installed."; return }
        var fresh = PipelineCanvas(name: "New Pipeline", nodes: [
            PipelineNode(section: first, x: 60, y: 90)
        ], edges: [], seedSection: first, promptTemplate: "{{idea}}", isDirty: true)
        fresh.originalData = nil
        if let url = store.savePipelinePreset(fresh) { reload(selecting: url) }
    }

    private func save() {
        guard let canvas else { return }
        if let url = store.savePipelinePreset(canvas, replacing: selectedRecord?.url) {
            note = "Saved \(canvas.name)."; reload(selecting: url)
        }
    }

    private func addNode(_ section: String) {
        let index = canvas!.nodes.count
        canvas!.nodes.append(PipelineNode(section: section,
            x: Double(60 + (index % 3) * 190), y: Double(70 + (index / 3) * 110)))
        canvas!.isDirty = true
    }

    private func moveNode(_ section: String, x: Double, y: Double) {
        guard let index = canvas?.nodes.firstIndex(where: { $0.section == section }) else { return }
        canvas!.nodes[index].x = x; canvas!.nodes[index].y = y; canvas!.isDirty = true
    }

    private func removeNode(_ section: String) {
        canvas!.nodes.removeAll { $0.section == section }
        canvas!.edges.removeAll { $0.source == section || $0.target == section }
        if canvas!.seedSection == section {
            canvas!.seedSection = canvas!.nodes.first?.section ?? ""
        }
        canvas!.isDirty = true
        if let selectedEdgeID,
           !canvas!.edges.contains(where: { $0.id == selectedEdgeID }) {
            self.selectedEdgeID = nil
        }
    }

    private func addEdge() {
        guard let source = canvas?.nodes.first?.section,
              let target = canvas?.nodes.dropFirst().first?.section else { return }
        let id = "edge-\(UUID().uuidString.lowercased())"
        canvas!.edges.append(PipelineEdge(id: id, source: source, target: target,
                                          artifactType: "idea"))
        canvas!.isDirty = true; selectedEdgeID = id
    }

    private func removeEdge(_ index: Int) {
        canvas!.edges.remove(at: index); canvas!.isDirty = true; selectedEdgeID = nil
    }

    private var runSheet: some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            Text("Run pipeline").font(DS.font.title)
            Text("Seeds exactly one session, then hands the saved preset to the real Conductor.")
                .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            TextField("Project", text: $runProject)
            TextField("Seed idea", text: $runIdea, axis: .vertical).lineLimit(5...10)
            HStack {
                Spacer(); Button("Cancel") { showRun = false }
                Button("Run") {
                    guard let canvas, let record = selectedRecord else { return }
                    var runURL = record.url
                    if canvas.isDirty {
                        guard let saved = store.savePipelinePreset(
                            canvas, replacing: record.url) else { return }
                        runURL = saved
                    }
                    if store.runPipeline(canvas, presetURL: runURL,
                                         project: runProject, idea: runIdea) != nil {
                        showRun = false; note = "Pipeline handed to Conductor."
                    }
                }.buttonStyle(.borderedProminent)
                    .disabled(runProject.trimmingCharacters(in: .whitespaces).isEmpty
                              || runIdea.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }.padding(DS.space.l).frame(width: 460)
    }
}
