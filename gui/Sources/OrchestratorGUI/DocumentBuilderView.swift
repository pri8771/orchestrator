import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct DocumentSlot: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
    let category: String
    let ownerSection: String?
}

struct DocumentCategory: Identifiable, Equatable, Sendable {
    let id: String
    let title: String
}

struct DocumentMap: Equatable, Sendable {
    let categories: [DocumentCategory]
    let slots: [DocumentSlot]
    var slotsByID: [String: DocumentSlot] {
        Dictionary(slots.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
    }
}

enum DocumentMapCodec {
    static func decode(_ data: Data) -> PipelineResult<DocumentMap> {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return .failure("doc_map.json is not a JSON object") }
        let categories = ((root["categories"] as? [[String: Any]]) ?? []).compactMap { item -> DocumentCategory? in
            guard let id = item["category_id"] as? String, !id.isEmpty else { return nil }
            return DocumentCategory(id: id, title: (item["title"] as? String) ?? id)
        }
        let slots = ((root["slots"] as? [[String: Any]]) ?? []).compactMap { item -> DocumentSlot? in
            guard let id = item["slot_id"] as? String, !id.isEmpty else { return nil }
            let owner = (item["owner_section"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
            return DocumentSlot(id: id,
                                title: (item["title"] as? String) ?? id,
                                category: (item["category"] as? String) ?? "uncategorized",
                                ownerSection: owner?.isEmpty == false ? owner : nil)
        }
        guard !categories.isEmpty, !slots.isEmpty else {
            return .failure("doc_map.json has no categories or slots")
        }
        return .success(DocumentMap(categories: categories, slots: slots))
    }
}

struct SituationCanvas: Equatable, Sendable {
    var name: String
    var slotIDs: [String]
    var rawRoot: [String: PipelineJSON]
    var originalData: Data?
    var isDirty = false
}

enum SituationCodec {
    static func decode(_ data: Data, nameHint: String = "") -> PipelineResult<SituationCanvas> {
        guard let any = try? JSONSerialization.jsonObject(with: data),
              let root = PipelineJSON(any)?.object else {
            return .failure("situation.json is not a JSON object")
        }
        let name = root["name"]?.string ?? nameHint
        guard !name.isEmpty else { return .failure("situation.name is missing") }
        guard let rawSlots = root["doc_slots"]?.array,
              rawSlots.allSatisfy({ $0.string != nil }) else {
            return .failure("situation.doc_slots must be an array of strings")
        }
        return .success(SituationCanvas(name: name,
                                        slotIDs: rawSlots.compactMap(\.string),
                                        rawRoot: root, originalData: data))
    }

    static func encode(_ canvas: SituationCanvas) -> PipelineResult<Data> {
        if !canvas.isDirty, let original = canvas.originalData { return .success(original) }
        var root = canvas.rawRoot
        root["doc_slots"] = .array(canvas.slotIDs.map(PipelineJSON.string))
        do {
            return .success(try JSONSerialization.data(
                withJSONObject: PipelineJSON.object(root).any,
                options: [.prettyPrinted, .sortedKeys]))
        } catch { return .failure("situation.json could not be encoded: \(error.localizedDescription)") }
    }
}

enum DocumentFillStatus: String, Equatable, Sendable {
    case neutral, filled, thin, empty, conflict, unavailable
}

struct GapReportSnapshot: Equatable, Sendable {
    let statuses: [String: DocumentFillStatus]
    let error: String?
}

enum GapReportParser {
    static func parse(_ text: String, map: DocumentMap) -> GapReportSnapshot {
        let countPattern = #"(?m)^- (Filled|Thin(?: \(under min_chars\))?|Empty|Lineage conflicts):\s*(\d+)"#
        guard let regex = try? NSRegularExpression(pattern: countPattern) else {
            return GapReportSnapshot(statuses: [:], error: "Gap report parser unavailable")
        }
        let ns = text as NSString
        let matches = regex.matches(in: text, range: NSRange(location: 0, length: ns.length))
        var counts: [String: Int] = [:]
        for match in matches where match.numberOfRanges == 3 {
            let key = ns.substring(with: match.range(at: 1))
                .replacingOccurrences(of: " (under min_chars)", with: "")
            counts[key] = Int(ns.substring(with: match.range(at: 2)))
        }
        let total = (counts["Filled"] ?? 0) + (counts["Thin"] ?? 0)
            + (counts["Empty"] ?? 0) + (counts["Lineage conflicts"] ?? 0)
        guard matches.count == 4, total == map.slots.count else {
            return GapReportSnapshot(statuses: [:], error: "GAP_REPORT.md is missing or does not match this blueprint")
        }
        var statuses = Dictionary(uniqueKeysWithValues: map.slots.map { ($0.id, DocumentFillStatus.filled) })
        for slot in map.slots {
            guard let line = text.split(separator: "\n").first(where: {
                $0.contains("**\(slot.title)**")
            })?.lowercased() else { continue }
            if line.contains("lineage conflict") || line.contains("lineage_conflict") {
                statuses[slot.id] = .conflict
            }
            else if line.contains("thin") { statuses[slot.id] = .thin }
            else { statuses[slot.id] = .empty }
        }
        return GapReportSnapshot(statuses: statuses, error: nil)
    }
}

struct SituationWorkflowPhase: Equatable, Sendable {
    let key: String
    let title: String
    let docSections: [String]
}

struct SituationImpact: Equatable, Sendable {
    let sections: [String]
    let phaseCount: Int
}

enum SituationImpactCompiler {
    // Pure port of situations.resolve_required_slots +
    // completeness.filter_phases_by_slots. Its parity test executes those
    // Python functions against the same fixture.
    static func preview(slotIDs: [String], map: DocumentMap,
                        phases: [SituationWorkflowPhase]) -> SituationImpact {
        let byID = map.slotsByID
        var seen = Set<String>()
        let known = slotIDs.filter { byID[$0] != nil && seen.insert($0).inserted }
        let sections = Array(Set(known.compactMap { byID[$0]?.ownerSection })).sorted()
        guard !known.isEmpty else { return SituationImpact(sections: sections, phaseCount: phases.count) }
        let required = Set(known)
        var kept = phases.filter { !required.isDisjoint(with: $0.docSections) }
        if let last = phases.last, !kept.contains(last) { kept.append(last) }
        if kept.count < min(3, phases.count) { kept = phases }
        return SituationImpact(sections: sections, phaseCount: kept.count)
    }
}

struct SituationFileRecord: Identifiable, Equatable {
    let name: String
    let url: URL
    let data: Data
    let error: String?
    var id: String { url.path }
}

enum SituationFileIO {
    static func changedOnDisk(isDirty: Bool, baseline: Data?, current: Data?) -> Bool {
        isDirty && baseline != nil && baseline != current
    }

    static func load(root: URL) -> [SituationFileRecord] {
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: root.path)) ?? []).sorted()
        return names.compactMap { name in
            let url = root.appendingPathComponent(name, isDirectory: true)
                .appendingPathComponent("situation.json")
            guard let data = try? Data(contentsOf: url) else { return nil }
            switch SituationCodec.decode(data, nameHint: name) {
            case .success(let canvas):
                return SituationFileRecord(name: canvas.name, url: url, data: data, error: nil)
            case .failure(let error):
                return SituationFileRecord(name: name, url: url, data: data, error: error)
            }
        }
    }

    static func save(_ canvas: SituationCanvas, to url: URL) throws -> Data {
        let data: Data
        switch SituationCodec.encode(canvas) {
        case .success(let value): data = value
        case .failure(let error): throw NSError(domain: "DocumentBuilder", code: 1,
                                                  userInfo: [NSLocalizedDescriptionKey: error])
        }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
        return data
    }
}

struct DocumentBuilderSheet: View {
    @EnvironmentObject private var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    @State private var records: [SituationFileRecord] = []
    @State private var selectedURL: URL?
    @State private var canvas: SituationCanvas?
    @State private var docMap: DocumentMap?
    @State private var loadError: String?
    @State private var selectedProjectName = ""
    @State private var gap = GapReportSnapshot(statuses: [:], error: nil)
    @State private var baselineData: Data?
    @State private var changedOnDisk = false

    private var selectedRecord: SituationFileRecord? { records.first { $0.url == selectedURL } }
    private var selectedProject: Project? { store.projects.first { $0.name == selectedProjectName } }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Document Builder").font(DS.font.headline)
                Spacer()
                Button("Close") { dismiss() }.accessibilityIdentifier("document-builder-close")
            }
            .padding(.horizontal, DS.space.m).frame(height: 44)
            Divider()
            HStack(spacing: 0) {
                situationList.frame(minWidth: 190, idealWidth: 220, maxWidth: 260)
                Divider()
                editor
            }
        }
        .frame(minWidth: 760, idealWidth: 1040, minHeight: 540, idealHeight: 700)
        .background(DS.windowBg)
        .task { reloadLibrary() }
        .onReceive(Timer.publish(every: 1.5, on: .main, in: .common).autoconnect()) { _ in checkExternalEdit() }
    }

    private var situationList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.xxs) {
                if records.isEmpty && loadError == nil {
                    Text("No Situations found").font(DS.font.body)
                    Text("Run the engine once to seed its Situation library.")
                        .font(DS.font.caption).foregroundStyle(.secondary)
                }
                ForEach(records) { record in
                    Button {
                        select(record)
                    } label: {
                        HStack {
                            Image(systemName: record.error == nil ? "doc.text" : "exclamationmark.triangle")
                            Text(record.name).lineLimit(1)
                            Spacer()
                        }
                        .padding(DS.space.xs)
                        .background(RoundedRectangle(cornerRadius: DS.radius.control)
                            .fill(selectedURL == record.url ? DS.accent.fill : Color.clear))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("situation-\(record.name)")
                }
            }.padding(DS.space.xs)
        }
    }

    @ViewBuilder private var editor: some View {
        if docMap == nil && loadError == nil {
            ProgressView("Loading document blueprint…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = loadError, canvas == nil {
            EmptyStateView(symbol: "exclamationmark.triangle", title: "Document builder unavailable", message: error)
        } else if let value = canvas, let map = docMap {
            VStack(spacing: 0) {
                if let error = selectedRecord?.error {
                    warningBanner("Corrupt situation.json: \(error). The file was left untouched; start a repair by adding slots.")
                }
                if changedOnDisk { warningBanner("This file changed on disk while you have edits. Revert to inspect it before applying.") }
                toolbar(value, map: map)
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.space.m) {
                        selectedFlow(value, map: map)
                        impact(value, map: map)
                        availableSlots(value, map: map)
                    }.padding(DS.space.m)
                }
            }
        } else {
            EmptyStateView(symbol: "doc.text", title: "Select a Situation",
                           message: "Choose a Situation to edit its document flow.")
        }
    }

    private func warningBanner(_ text: String) -> some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(DS.font.caption).foregroundStyle(DS.status.warning.color)
            .padding(DS.space.xs).frame(maxWidth: .infinity, alignment: .leading)
            .background(DS.status.warning.fill)
    }

    private func toolbar(_ value: SituationCanvas, map: DocumentMap) -> some View {
        HStack {
            Text(value.name).font(DS.font.title)
            Spacer()
            Picker("Project", selection: $selectedProjectName) {
                Text("No project selected").tag("")
                ForEach(store.projects) { Text($0.name).tag($0.name) }
            }
            .frame(maxWidth: 240).accessibilityIdentifier("document-project-picker")
            .onChange(of: selectedProjectName) { _, _ in loadGap(map: map) }
            Button("Revert") { if let record = selectedRecord { select(record) } }
                .disabled(!value.isDirty && !changedOnDisk)
                .accessibilityIdentifier("document-revert")
            Button("Apply") { apply(value) }
                .disabled(!value.isDirty || changedOnDisk)
                .keyboardShortcut(.defaultAction)
                .accessibilityIdentifier("document-apply")
        }.padding(.horizontal, DS.space.m).frame(height: 50)
    }

    private func selectedFlow(_ value: SituationCanvas, map: DocumentMap) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Selected flow").font(DS.font.headline)
            if value.slotIDs.isEmpty {
                Text("No slots selected — this Situation runs nothing doc-driven.")
                    .font(DS.font.body).foregroundStyle(.secondary)
                    .padding(DS.space.m).frame(maxWidth: .infinity)
                    .overlay(RoundedRectangle(cornerRadius: DS.radius.card).stroke(DS.hairline))
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180), spacing: DS.space.xs)], spacing: DS.space.xs) {
                    ForEach(Array(value.slotIDs.enumerated()), id: \.offset) { index, id in
                        slotChip(id: id, map: map, selected: true) {
                            mutate { $0.slotIDs.remove(at: index) }
                        }
                        .draggable(id)
                        .dropDestination(for: String.self) { ids, _ in
                            guard let moving = ids.first else { return false }
                            mutate { state in
                                state.slotIDs.removeAll { $0 == moving }
                                state.slotIDs.insert(moving, at: min(index, state.slotIDs.count))
                            }
                            return true
                        }
                    }
                }
            }
        }
        .dropDestination(for: String.self) { ids, _ in
            guard let id = ids.first else { return false }
            mutate { state in if !state.slotIDs.contains(id) { state.slotIDs.append(id) } }
            return true
        }
    }

    private func availableSlots(_ value: SituationCanvas, map: DocumentMap) -> some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            Text("Blueprint slots").font(DS.font.headline)
            ForEach(map.categories) { category in
                VStack(alignment: .leading, spacing: DS.space.xs) {
                    Text(category.title).font(DS.font.body.weight(.semibold))
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 180), spacing: DS.space.xs)], spacing: DS.space.xs) {
                        ForEach(map.slots.filter { $0.category == category.id }) { slot in
                            slotChip(id: slot.id, map: map, selected: false) {
                                mutate { state in if !state.slotIDs.contains(slot.id) { state.slotIDs.append(slot.id) } }
                            }.draggable(slot.id)
                        }
                    }
                }
            }
        }
    }

    private func slotChip(id: String, map: DocumentMap, selected: Bool,
                          action: @escaping () -> Void) -> some View {
        let slot = map.slotsByID[id]
        let owner = slot?.ownerSection
        let status = selectedProject == nil ? DocumentFillStatus.neutral
            : (gap.statuses[id] ?? .unavailable)
        return Button(action: action) {
            HStack(spacing: DS.space.xs) {
                Image(systemName: owner == nil ? "exclamationmark.triangle.fill" : statusSymbol(status))
                    .foregroundStyle(owner == nil ? DS.status.warning.color : statusColor(status))
                VStack(alignment: .leading, spacing: 2) {
                    Text(slot?.title ?? id).font(DS.font.body).lineLimit(2)
                    Text(owner.map { "Owner: \($0) · \(statusLabel(status))" }
                         ?? "Unowned — fix doc_map.json")
                        .font(DS.font.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                Image(systemName: selected ? "minus.circle" : "plus.circle")
            }
            .padding(DS.space.xs).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: DS.radius.control).fill(.quaternary.opacity(0.5)))
            .overlay(RoundedRectangle(cornerRadius: DS.radius.control).stroke(owner == nil ? DS.status.warning.color : DS.hairline))
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("document-slot-\(id)-\(selected ? "remove" : "add")")
        .accessibilityLabel("\(slot?.title ?? id), \(owner.map { "owner \($0)" } ?? "unowned"), \(statusLabel(status))")
    }

    private func impact(_ value: SituationCanvas, map: DocumentMap) -> some View {
        let phases = store.situationWorkflowPhases(named: selectedProject?.workflow ?? "app_build")
        let preview = SituationImpactCompiler.preview(slotIDs: value.slotIDs, map: map, phases: phases)
        return VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Impact preview").font(DS.font.headline)
            Text(preview.sections.isEmpty ? "No owning sections · ~\(preview.phaseCount) phases"
                 : "Will run \(preview.sections.joined(separator: ", ")) · ~\(preview.phaseCount) phases")
                .font(DS.font.body)
            if selectedProject == nil {
                Text("No project selected — fill status is neutral; phase count uses app_build.")
                    .font(DS.font.caption).foregroundStyle(.secondary)
            } else if let error = gap.error {
                Text(error).font(DS.font.caption).foregroundStyle(DS.status.warning.color)
            }
        }.padding(DS.space.s).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: DS.radius.card).fill(DS.accent.fill))
    }

    private func reloadLibrary() {
        if let seedError = store.ensureSituationSeeds() { loadError = seedError }
        switch store.readDocumentMap() {
        case .failure(let error): loadError = error
        case .success(let map): docMap = map
        }
        records = store.readSituationFiles()
        if let first = records.first { select(first) }
    }

    private func select(_ record: SituationFileRecord) {
        selectedURL = record.url; baselineData = record.data; changedOnDisk = false
        switch SituationCodec.decode(record.data, nameHint: record.name) {
        case .success(let value): canvas = value
        case .failure:
            canvas = SituationCanvas(name: record.name, slotIDs: [], rawRoot: [
                "schema_version": .number(1), "name": .string(record.name),
                "description": .string(""), "pipeline_ref": .string(""),
                "overrides": .object(["sections": .object([:]), "phases": .object([:]), "casts": .object([:])])
            ], originalData: nil)
        }
        if let map = docMap { loadGap(map: map) }
    }

    private func mutate(_ body: (inout SituationCanvas) -> Void) {
        guard var value = canvas else { return }
        body(&value); value.isDirty = true; canvas = value
    }

    private func apply(_ value: SituationCanvas) {
        guard let url = selectedURL else { return }
        do {
            let data = try store.writeSituation(value, to: url)
            baselineData = data; changedOnDisk = false
            var saved = value; saved.originalData = data; saved.isDirty = false; canvas = saved
            records = store.readSituationFiles()
        } catch { loadError = error.localizedDescription }
    }

    private func checkExternalEdit() {
        guard canvas?.isDirty == true, let url = selectedURL, let baselineData else { return }
        let current = try? Data(contentsOf: url)
        changedOnDisk = SituationFileIO.changedOnDisk(
            isDirty: true, baseline: baselineData, current: current)
    }

    private func loadGap(map: DocumentMap) {
        guard let project = selectedProject else {
            gap = GapReportSnapshot(statuses: [:], error: nil); return
        }
        let url = project.dirURL.appendingPathComponent("docs/GAP_REPORT.md")
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            gap = GapReportSnapshot(statuses: [:], error: "No engine-rendered GAP_REPORT.md for this project")
            return
        }
        gap = GapReportParser.parse(text, map: map)
    }

    private func statusLabel(_ status: DocumentFillStatus) -> String {
        status == .neutral ? "no project selected" : status.rawValue
    }
    private func statusSymbol(_ status: DocumentFillStatus) -> String {
        switch status {
        case .filled: return "checkmark.circle.fill"
        case .thin: return "circle.lefthalf.filled"
        case .empty: return "circle"
        case .conflict: return "exclamationmark.arrow.triangle.2.circlepath"
        case .neutral: return "minus.circle"
        case .unavailable: return "questionmark.circle"
        }
    }
    private func statusColor(_ status: DocumentFillStatus) -> Color {
        switch status {
        case .filled: return DS.status.success.color
        case .thin, .conflict: return DS.status.warning.color
        case .empty: return DS.status.error.color
        case .neutral, .unavailable: return DS.status.idle
        }
    }
}
