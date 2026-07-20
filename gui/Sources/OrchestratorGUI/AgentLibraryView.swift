import Foundation
import SwiftUI

struct SamplingPresetDef: Identifiable, Equatable {
    var id: String
    var label: String
    var params: [String: Double]
    var notes: String
}

struct AgentPersonaDef: Identifiable, Equatable {
    var id: String
    var name: String
    var preamble: String
    var backend: String
    var model: String
    var defaultEffort: String
    var preset: String
}

struct RecommendedCastDef: Equatable {
    var count: Int
    var coordinator: Bool
    var note: String

    var label: String {
        var pieces = ["\(count) \(note.isEmpty ? "agents" : note)"]
        if coordinator { pieces.append("coordinator") }
        return pieces.joined(separator: " + ")
    }
}

struct AgentLibraryDocument: Equatable {
    var personas: [AgentPersonaDef] = []
    var presets: [SamplingPresetDef] = []
    var recommendedCasts: [String: RecommendedCastDef] = [:]
    var warnings: [String] = []

    static func loadLayered(fleetURL: URL, sectionURL: URL? = nil) -> AgentLibraryDocument {
        var result = AgentLibraryDocument()
        var personaMap: [String: AgentPersonaDef] = [:]
        var presetMap: [String: SamplingPresetDef] = [:]
        let fleetPresetDir = fleetURL.deletingLastPathComponent().appendingPathComponent("presets")
        let sectionPresetDir = sectionURL?.deletingLastPathComponent().appendingPathComponent("presets")
        for dir in [fleetPresetDir, sectionPresetDir].compactMap({ $0 }) {
            guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { continue }
            for name in names.sorted() where name.hasSuffix(".json") {
                let url = dir.appendingPathComponent(name)
                guard let data = try? Data(contentsOf: url),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let id = obj["id"] as? String, !id.isEmpty,
                      let label = obj["label"] as? String,
                      let rawParams = obj["params"] as? [String: Any] else {
                    result.warnings.append("\(url.path) is corrupt or incomplete")
                    continue
                }
                var params: [String: Double] = [:]
                for (key, value) in rawParams {
                    if let number = value as? NSNumber { params[key] = number.doubleValue }
                }
                presetMap[id] = SamplingPresetDef(id: id, label: label, params: params,
                                                   notes: obj["notes"] as? String ?? "")
            }
        }
        for url in [fleetURL, sectionURL].compactMap({ $0 }) {
            guard FileManager.default.fileExists(atPath: url.path) else { continue }
            guard let data = try? Data(contentsOf: url),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                result.warnings.append("\(url.path) is corrupt")
                continue
            }
            for raw in (obj["personas"] as? [[String: Any]]) ?? [] {
                guard let id = raw["id"] as? String, !id.isEmpty,
                      let name = raw["name"] as? String,
                      let preamble = raw["preamble"] as? String else {
                    result.warnings.append("\(url.path) contains an incomplete persona")
                    continue
                }
                let persona = AgentPersonaDef(
                    id: id, name: name, preamble: preamble,
                    backend: raw["backend"] as? String ?? "",
                    model: raw["model"] as? String ?? "",
                    defaultEffort: raw["default_effort"] as? String ?? "",
                    preset: raw["preset"] as? String ?? "")
                if !persona.defaultEffort.isEmpty && !supportsEffort(persona.backend) {
                    result.warnings.append("Persona \(id): \(persona.backend) has no effort control")
                }
                if !persona.preset.isEmpty && !supportsSampling(persona.backend) {
                    result.warnings.append("Persona \(id): \(persona.backend) has no sampling support")
                }
                if !persona.preset.isEmpty && presetMap[persona.preset] == nil {
                    result.warnings.append("Persona \(id) references missing preset \(persona.preset)")
                }
                personaMap[id] = persona
            }
            for (key, raw) in (obj["recommended_casts"] as? [String: Any]) ?? [:] {
                guard let value = raw as? [String: Any],
                      let count = value["count"] as? Int else { continue }
                result.recommendedCasts[key] = RecommendedCastDef(
                    count: count, coordinator: value["coordinator"] as? Bool ?? false,
                    note: value["note"] as? String ?? "")
            }
        }
        result.personas = personaMap.values.sorted { $0.id < $1.id }
        result.presets = presetMap.values.sorted { $0.id < $1.id }
        return result
    }

    func recommendedHint(forPhase key: String) -> String? {
        let lower = key.lowercased()
        let kind: String
        if lower.contains("verif") || lower.contains("review") || lower.contains("audit") {
            kind = "verification"
        } else if lower.contains("idea") || lower.contains("discuss") || lower.contains("research") {
            kind = "ideation"
        } else if lower.contains("build") || lower.contains("implement") || lower.contains("mechanical") {
            kind = "mechanical"
        } else {
            kind = "synthesis"
        }
        return recommendedCasts[kind]?.label
    }

    static func supportsEffort(_ backend: String) -> Bool {
        backend == "claude" || backend == "codex" || backend.hasPrefix("local:")
    }

    static func supportsSampling(_ backend: String) -> Bool {
        backend.hasPrefix("local:") || backend.hasPrefix("api:")
    }

    static func save(personas: [AgentPersonaDef], to url: URL) -> String? {
        for persona in personas {
            if persona.id.trimmingCharacters(in: .whitespaces).isEmpty
                || persona.name.trimmingCharacters(in: .whitespaces).isEmpty
                || persona.preamble.trimmingCharacters(in: .whitespaces).isEmpty {
                return "Persona id, name, and preamble are required."
            }
            if !persona.defaultEffort.isEmpty && !supportsEffort(persona.backend) {
                return "\(persona.backend) has no effort control; clear default effort."
            }
            if !persona.preset.isEmpty && !supportsSampling(persona.backend) {
                return "\(persona.backend) has no sampling-parameter support; clear preset."
            }
        }
        var root: [String: Any] = [:]
        if let data = try? Data(contentsOf: url),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            root = existing
        }
        root["schema_version"] = 1
        root["personas"] = personas.map { persona in
            var raw: [String: Any] = ["id": persona.id, "name": persona.name,
                                      "preamble": persona.preamble]
            if !persona.backend.isEmpty { raw["backend"] = persona.backend }
            if !persona.model.isEmpty { raw["model"] = persona.model }
            if !persona.defaultEffort.isEmpty { raw["default_effort"] = persona.defaultEffort }
            if !persona.preset.isEmpty { raw["preset"] = persona.preset }
            return raw
        }
        guard let data = try? JSONSerialization.data(withJSONObject: root,
                                                     options: [.prettyPrinted, .sortedKeys]) else {
            return "Agent Library could not be encoded."
        }
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: url, options: .atomic)
            return nil
        } catch { return error.localizedDescription }
    }
}

struct AgentLibraryView: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    @State private var scope = "fleet"
    @State private var document = AgentLibraryDocument()
    @State private var draft = AgentPersonaDef(id: "", name: "", preamble: "",
                                               backend: "claude", model: "",
                                               defaultEffort: "", preset: "")
    @State private var editingID: String?
    @State private var error = ""

    private var sectionNames: [String] {
        let root = store.orchDirURL.appendingPathComponent("sections")
        return ((try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? [])
            .filter { FileManager.default.fileExists(atPath: root.appendingPathComponent($0).path) }
            .sorted()
    }

    private var libraryURL: URL {
        scope == "fleet" ? store.orchDirURL.appendingPathComponent("agent_library.json")
            : store.orchDirURL.appendingPathComponent("sections/\(scope)/agent_library.json")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            HStack {
                Text("Agent Library").font(DS.font.title)
                Picker("Scope", selection: $scope) {
                    Text("Fleet").tag("fleet")
                    ForEach(sectionNames, id: \.self) { Text($0.capitalized).tag($0) }
                }
                .frame(width: 190)
                Spacer()
                Button("Done") { dismiss() }
            }
            if !document.warnings.isEmpty {
                InlineBanner(kind: .warning, title: "Library needs attention",
                             message: document.warnings.joined(separator: "\n")) { EmptyView() }
            }
            if !error.isEmpty {
                Text(error).font(DS.font.caption).foregroundStyle(DS.status.error.color)
            }
            HSplitView {
                List(selection: $editingID) {
                    ForEach(document.personas) { persona in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(persona.name).font(DS.font.callout)
                            Text("\(persona.id) · \(persona.backend)")
                                .font(DS.font.caption2).foregroundStyle(.secondary)
                        }.tag(persona.id)
                    }
                }
                .frame(minWidth: 220)
                editor
            }
        }
        .padding(DS.space.m)
        .frame(minWidth: 760, minHeight: 540)
        .onAppear(perform: reload)
        .onChange(of: scope) { _, _ in reload() }
        .onChange(of: editingID) { _, id in
            if let hit = document.personas.first(where: { $0.id == id }) { draft = hit }
        }
    }

    private var editor: some View {
        Form {
            TextField("ID", text: $draft.id)
            TextField("Name", text: $draft.name)
            TextField("Preamble", text: $draft.preamble, axis: .vertical)
            Picker("Backend", selection: $draft.backend) {
                ForEach(["claude", "codex", "gemini", "ollama", "local:qwen3",
                         "api:openai", "api:anthropic", "api:google"], id: \.self) { Text($0) }
            }
            TextField("Model", text: $draft.model)
            Picker("Default effort", selection: $draft.defaultEffort) {
                Text("Backend default").tag("")
                ForEach(["low", "medium", "high"], id: \.self) { Text($0.capitalized).tag($0) }
            }
            .disabled(!AgentLibraryDocument.supportsEffort(draft.backend))
            Text(AgentLibraryDocument.supportsEffort(draft.backend)
                 ? "The selected backend honors effort."
                 : "This backend has no effort control.")
                .font(DS.font.caption).foregroundStyle(.secondary)
            Picker("Sampling preset", selection: $draft.preset) {
                Text("None").tag("")
                ForEach(document.presets) { preset in Text(preset.label).tag(preset.id) }
            }
            .disabled(!AgentLibraryDocument.supportsSampling(draft.backend))
            if let preset = document.presets.first(where: { $0.id == draft.preset }) {
                Text(preset.params.keys.sorted().map { "\($0)=\(preset.params[$0] ?? 0)" }
                    .joined(separator: " · "))
                    .font(DS.font.caption).foregroundStyle(.secondary)
            } else if !AgentLibraryDocument.supportsSampling(draft.backend) {
                Text("Sampling presets are unavailable for this backend.")
                    .font(DS.font.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("New") {
                    editingID = nil
                    draft = AgentPersonaDef(id: "", name: "", preamble: "",
                                            backend: "claude", model: "",
                                            defaultEffort: "", preset: "")
                }
                if editingID != nil {
                    Button("Delete", role: .destructive) {
                        document.personas.removeAll { $0.id == editingID }
                        persist()
                        editingID = nil
                    }
                }
                Spacer()
                Button("Save") { saveDraft() }.buttonStyle(.borderedProminent)
            }
        }.formStyle(.grouped)
    }

    private func reload() {
        let fleet = store.orchDirURL.appendingPathComponent("agent_library.json")
        let section = scope == "fleet" ? nil : libraryURL
        var loaded = AgentLibraryDocument.loadLayered(fleetURL: fleet, sectionURL: section)
        if scope != "fleet" {
            // Edit only this layer: inherited fleet personas remain visible to
            // resolution but must not be copied into the section on save.
            loaded.personas = AgentLibraryDocument.loadLayered(
                fleetURL: libraryURL).personas
        }
        document = loaded
        editingID = document.personas.first?.id
        if let first = document.personas.first { draft = first }
        error = ""
    }

    private func saveDraft() {
        if !draft.preset.isEmpty && !document.presets.contains(where: { $0.id == draft.preset }) {
            error = "Preset \(draft.preset) is missing. Choose an available preset or None."
            return
        }
        var personas = document.personas
        if let index = personas.firstIndex(where: { $0.id == editingID || $0.id == draft.id }) {
            personas[index] = draft
        } else { personas.append(draft) }
        document.personas = personas.sorted { $0.id < $1.id }
        persist()
        editingID = draft.id
    }

    private func persist() {
        error = AgentLibraryDocument.save(personas: document.personas, to: libraryURL) ?? ""
        if error.isEmpty { reload() }
    }
}
