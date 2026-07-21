import Foundation
import SwiftUI

struct SituationApplyDiff: Equatable, Sendable {
    let phasesAdded: [String]
    let phasesRemoved: [String]
    let sectionsActivated: [String]
    let sectionsDeactivated: [String]
    let slotDelta: Int
    var hasChanges: Bool {
        !phasesAdded.isEmpty || !phasesRemoved.isEmpty
            || !sectionsActivated.isEmpty || !sectionsDeactivated.isEmpty || slotDelta != 0
    }
}

enum SituationApplyService {
    static func diff(beforeSlots: [String], afterSlots: [String], map: DocumentMap,
                     phases: [SituationWorkflowPhase]) -> SituationApplyDiff {
        let before = SituationImpactCompiler.preview(slotIDs: beforeSlots, map: map, phases: phases)
        let after = SituationImpactCompiler.preview(slotIDs: afterSlots, map: map, phases: phases)
        let requiredBefore = before.phaseKeys
        let requiredAfter = after.phaseKeys
        return SituationApplyDiff(
            phasesAdded: requiredAfter.filter { !requiredBefore.contains($0) },
            phasesRemoved: requiredBefore.filter { !requiredAfter.contains($0) },
            sectionsActivated: after.sections.filter { !before.sections.contains($0) },
            sectionsDeactivated: before.sections.filter { !after.sections.contains($0) },
            slotDelta: Set(afterSlots).count - Set(beforeSlots).count)
    }

    @discardableResult
    static func confirm(situation name: String, runConfigURL: URL) throws -> Bool {
        let fm = FileManager.default
        var root: [String: Any] = [:]
        if fm.fileExists(atPath: runConfigURL.path) {
            let data = try Data(contentsOf: runConfigURL)
            guard let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw NSError(domain: "SituationApply", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "run_config.json is not an object"])
            }
            root = decoded
        }
        if root["situation"] as? String == name { return false }
        root["situation"] = name
        let data = try JSONSerialization.data(withJSONObject: root,
                                               options: [.prettyPrinted, .sortedKeys])
        try fm.createDirectory(at: runConfigURL.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
        try data.write(to: runConfigURL, options: .atomic)
        return true
    }

    static func currentRef(runConfigURL: URL) -> String? {
        guard let data = try? Data(contentsOf: runConfigURL),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return root["situation"] as? String
    }
}

enum SituationEngineQuery {
    static func diff(python: String, moduleRoot: URL, orchDir: URL,
                     projectDir: URL, workflow: String,
                     candidate: String) -> PipelineResult<SituationApplyDiff> {
        let script = """
import json,sys
sys.path.insert(0,sys.argv[1])
import completeness,situations,workflows,docs
orch,project,workflow_name,candidate=sys.argv[2:]
rc=completeness.load_run_config(project)
ph=list(workflows.load_workflow(workflow_name,orch).phases)
ph=completeness.filter_phases(ph,rc.get('completeness'),on_warn=lambda _m:None)
dm=docs.load_doc_map(orch,on_warn=lambda _m:None)
def calc(ref):
 if not ref: return {'phases':[p.key for p in ph],'sections':[],'slots':[]}
 s=situations.load_situation(ref,orch,on_error=lambda _m:None)
 if s is None: return {'phases':[p.key for p in ph],'sections':[],'slots':[]}
 slots,owners=situations.resolve_required_slots(s,dm)
 chosen=completeness.filter_phases_by_slots(ph,slots,on_warn=lambda _m:None)
 return {'phases':[p.key for p in chosen],'sections':sorted(owners),'slots':slots}
b=calc(rc.get('situation')); a=calc(candidate)
print(json.dumps({'phases_added':[x for x in a['phases'] if x not in b['phases']],
'phases_removed':[x for x in b['phases'] if x not in a['phases']],
'sections_activated':[x for x in a['sections'] if x not in b['sections']],
'sections_deactivated':[x for x in b['sections'] if x not in a['sections']],
'slot_delta':len(a['slots'])-len(b['slots'])}))
"""
        let process = Process(); process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = moduleRoot
        process.arguments = ["-c", script, moduleRoot.path, orchDir.path,
                             projectDir.path, workflow, candidate]
        let output = Pipe(), errors = Pipe()
        process.standardOutput = output; process.standardError = errors
        do { try process.run(); process.waitUntilExit() }
        catch { return .failure(error.localizedDescription) }
        guard process.terminationStatus == 0 else {
            let data = errors.fileHandleForReading.readDataToEndOfFile()
            return .failure(String(data: data, encoding: .utf8) ?? "Situation impact query failed")
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return .failure("Situation impact query returned invalid JSON")
        }
        return .success(SituationApplyDiff(
            phasesAdded: root["phases_added"] as? [String] ?? [],
            phasesRemoved: root["phases_removed"] as? [String] ?? [],
            sectionsActivated: root["sections_activated"] as? [String] ?? [],
            sectionsDeactivated: root["sections_deactivated"] as? [String] ?? [],
            slotDelta: root["slot_delta"] as? Int ?? 0))
    }
}

enum SituationLibraryNaming {
    static func copyName(source: String, existingNames: Set<String>,
                         occupiedSlugs: Set<String>) -> String {
        var candidate = "\(source)-copy", number = 2
        while existingNames.contains(candidate)
                || occupiedSlugs.contains(OrchestratorStore.slugify(candidate)) {
            candidate = "\(source)-copy-\(number)"; number += 1
        }
        return candidate
    }
}

enum SituationEditCodec {
    static let knownPhaseFields = Set([
        "rounds", "claude", "claude_reasoning", "codex", "codex_reasoning",
        "gemini", "gemini_reasoning", "ollama", "ollama_reasoning", "agents", "composition"
    ])

    static func pipelineRef(_ canvas: SituationCanvas) -> String {
        canvas.rawRoot["pipeline_ref"]?.string ?? ""
    }

    static func pipelineIssue(ref: String, availableNames: Set<String>) -> String? {
        guard !ref.isEmpty, !availableNames.contains(ref) else { return nil }
        return "Situation references ‘\(ref)’, but that preset does not exist."
    }

    static func setPipelineRef(_ value: String, in canvas: inout SituationCanvas) {
        canvas.rawRoot["pipeline_ref"] = .string(value); canvas.isDirty = true
    }

    static func overrides(_ canvas: SituationCanvas) -> [String: PipelineJSON] {
        canvas.rawRoot["overrides"]?.object ?? [:]
    }

    static func setSection(_ section: String, enabled: Bool, in canvas: inout SituationCanvas) {
        var overrides = overrides(canvas)
        var sections = overrides["sections"]?.object ?? [:]
        var entry = sections[section]?.object ?? [:]
        entry["enabled"] = .bool(enabled); sections[section] = .object(entry)
        overrides["sections"] = .object(sections)
        canvas.rawRoot["overrides"] = .object(overrides); canvas.isDirty = true
    }

    static func setPhaseField(_ phase: String, field: String, value: PipelineJSON?,
                              in canvas: inout SituationCanvas) {
        guard knownPhaseFields.contains(field) else { return }
        var overrides = overrides(canvas)
        var phases = overrides["phases"]?.object ?? [:]
        var entry = phases[phase]?.object ?? [:]
        if let value { entry[field] = value } else { entry.removeValue(forKey: field) }
        phases[phase] = .object(entry); overrides["phases"] = .object(phases)
        canvas.rawRoot["overrides"] = .object(overrides); canvas.isDirty = true
    }

    static func setCast(_ phase: String, ids: [String], in canvas: inout SituationCanvas) {
        var overrides = overrides(canvas)
        var casts = overrides["casts"]?.object ?? [:]
        if ids.isEmpty { casts.removeValue(forKey: phase) }
        else { casts[phase] = .array(ids.map(PipelineJSON.string)) }
        overrides["casts"] = .object(casts)
        canvas.rawRoot["overrides"] = .object(overrides); canvas.isDirty = true
    }

    static func unknownPhaseFields(_ canvas: SituationCanvas) -> [String] {
        let phases = overrides(canvas)["phases"]?.object ?? [:]
        return phases.flatMap { phase, value in
            (value.object ?? [:]).keys.filter { !knownPhaseFields.contains($0) }
                .map { "\(phase).\($0)" }
        }.sorted()
    }
}

struct SituationEditorSheet: View {
    @EnvironmentObject private var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    @State private var records: [SituationFileRecord] = []
    @State private var selectedURL: URL?
    @State private var canvas: SituationCanvas?
    @State private var map: DocumentMap?
    @State private var tab = "documents"
    @State private var note: String?
    @State private var applyProject: Project?
    @State private var applyDiff: SituationApplyDiff?
    @State private var presetRecords: [PipelinePresetRecord] = []

    private var selectedRecord: SituationFileRecord? { records.first { $0.url == selectedURL } }
    private var selectedRef: String? {
        selectedRecord?.engineRef
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Situations").font(DS.font.headline)
                if let note { Text(note).font(DS.font.caption).foregroundStyle(.secondary) }
                Spacer()
                Button("Close") { dismiss() }.accessibilityIdentifier("situation-editor-close")
            }.padding(.horizontal, DS.space.m).frame(height: 44)
            Divider()
            HSplitView {
                library.frame(minWidth: 210, idealWidth: 240)
                editor.frame(minWidth: 620)
            }
        }
        .frame(minWidth: 900, minHeight: 650)
        .background(DS.windowBg)
        .task { reload() }
        .sheet(item: $applyProject) { project in applySheet(project) }
    }

    private var library: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack {
                Text("Library").font(DS.font.headline)
                Spacer()
                Button { duplicateSelected() } label: { Image(systemName: "plus.square.on.square") }
                    .disabled(canvas == nil).accessibilityIdentifier("situation-duplicate")
            }
            ScrollView {
                VStack(spacing: DS.space.xxs) {
                    ForEach(records) { record in
                        Button { select(record) } label: {
                            HStack {
                                Image(systemName: record.error == nil ? "square.stack.3d.up" : "exclamationmark.triangle")
                                Text(record.name).lineLimit(1); Spacer()
                            }.padding(DS.space.xs)
                                .background(RoundedRectangle(cornerRadius: DS.radius.control)
                                    .fill(selectedURL == record.url ? DS.accent.fill : Color.clear))
                        }.buttonStyle(.plain).accessibilityIdentifier("situation-library-\(record.name)")
                    }
                }
            }
            Button("Move to Trash", role: .destructive) { deleteSelected() }
                .disabled(canvas == nil).accessibilityIdentifier("situation-delete")
        }.padding(DS.space.s)
    }

    @ViewBuilder private var editor: some View {
        if let canvas, let record = selectedRecord {
            VStack(spacing: 0) {
                HStack {
                    Text(canvas.name).font(DS.font.title)
                    Spacer()
                    Menu("Apply to project…") {
                        ForEach(store.projects) { project in
                            Button(project.name) { prepareApply(project) }
                        }
                    }.disabled(canvas.isDirty || map == nil)
                        .help(canvas.isDirty ? "Save the Situation before previewing its on-disk effect." : "Preview and apply")
                        .accessibilityIdentifier("situation-apply-menu")
                    Button("Save Situation") { save() }
                        .disabled(!canvas.isDirty).accessibilityIdentifier("situation-save")
                }.padding(.horizontal, DS.space.m).frame(height: 50)
                Picker("Editor", selection: $tab) {
                    Text("Document flow").tag("documents")
                    Text("Pipeline").tag("pipeline")
                    Text("Overrides").tag("overrides")
                }.pickerStyle(.segmented).padding(.horizontal, DS.space.m)
                    .accessibilityIdentifier("situation-editor-tabs")
                Divider().padding(.top, DS.space.xs)
                switch tab {
                case "pipeline": pipelineTab(canvas)
                case "overrides": overridesTab(canvas)
                default:
                    DocumentBuilderSheet(initialSituationURL: record.url, compact: true) {
                        reload(selecting: record.url)
                    }.environmentObject(store).id(record.url.path)
                }
            }
        } else {
            EmptyStateView(symbol: "square.stack.3d.up", title: "Select a Situation",
                           message: "Choose a library entry to edit its document flow, pipeline, and overrides.")
        }
    }

    private func pipelineTab(_ value: SituationCanvas) -> some View {
        let ref = SituationEditCodec.pipelineRef(value)
        let names = presetRecords.map(\.name)
        let issue = SituationEditCodec.pipelineIssue(ref: ref, availableNames: Set(names))
        return VStack(alignment: .leading, spacing: DS.space.s) {
            Picker("Pipeline preset", selection: Binding(get: { ref }, set: { newValue in
                mutate { SituationEditCodec.setPipelineRef(newValue, in: &$0) }
            })) {
                Text("No pipeline").tag("")
                ForEach(names, id: \.self) { Text($0).tag($0) }
                if issue != nil { Text("Missing: \(ref)").tag(ref) }
            }.accessibilityIdentifier("situation-pipeline-picker")
            if let issue {
                InlineBanner(kind: .error, title: "Pipeline preset is missing",
                             message: issue)
            } else if ref.isEmpty {
                EmptyStateView(symbol: "point.3.connected.trianglepath.dotted",
                               title: "No pipeline selected",
                               message: "Choose a preset to include cross-section routing in this Situation.")
            } else {
                PipelineBuilderSheet(initialPresetName: ref, compact: true)
                    .environmentObject(store).id(ref)
            }
        }.padding(DS.space.m)
    }

    private func overridesTab(_ value: SituationCanvas) -> some View {
        let sections = store.knownPipelineSections().sorted()
        let phases = store.allSituationWorkflowPhases()
        let unknown = SituationEditCodec.unknownPhaseFields(value)
        return ScrollView {
            VStack(alignment: .leading, spacing: DS.space.m) {
                Text("Section activation").font(DS.font.headline)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180))]) {
                    ForEach(sections, id: \.self) { section in
                        Toggle(section.capitalized, isOn: sectionBinding(section))
                            .accessibilityIdentifier("situation-section-\(section)")
                    }
                }
                Text("Phase routing & casts").font(DS.font.headline)
                ForEach(phases, id: \.key) { phase in phaseRow(phase, canvas: value) }
                if !unknown.isEmpty {
                    InlineBanner(kind: .warning, title: "Unknown fields preserved read-only",
                                 message: unknown.joined(separator: ", "))
                }
            }.padding(DS.space.m)
        }
    }

    private func phaseRow(_ phase: SituationWorkflowPhase, canvas value: SituationCanvas) -> some View {
        let phaseObj = SituationEditCodec.overrides(value)["phases"]?.object?[phase.key]?.object ?? [:]
        let casts = SituationEditCodec.overrides(value)["casts"]?.object?[phase.key]?.array?.compactMap(\.string) ?? []
        return VStack(alignment: .leading, spacing: DS.space.xs) {
            Text(phase.title).font(DS.font.body.weight(.semibold))
            ViewThatFits(in: .horizontal) {
                HStack { phaseFields(phase.key, object: phaseObj, casts: casts) }
                VStack { phaseFields(phase.key, object: phaseObj, casts: casts) }
            }
        }.padding(DS.space.s).background(RoundedRectangle(cornerRadius: DS.radius.card).fill(DS.raised))
    }

    @ViewBuilder private func phaseFields(_ key: String, object: [String: PipelineJSON], casts: [String]) -> some View {
        Stepper("Rounds \(object["rounds"]?.int ?? 0)", value: intBinding(key, "rounds"), in: 0...99)
            .accessibilityIdentifier("situation-rounds-\(key)")
        TextField("Agents", text: stringBinding(key, "agents")).frame(minWidth: 100)
            .accessibilityIdentifier("situation-agents-\(key)")
        TextField("Composition", text: stringBinding(key, "composition")).frame(minWidth: 110)
            .accessibilityIdentifier("situation-composition-\(key)")
        TextField("Codex model", text: stringBinding(key, "codex")).frame(minWidth: 120)
            .accessibilityIdentifier("situation-codex-\(key)")
        effortPicker("Codex effort", phase: key, field: "codex_reasoning")
        TextField("Claude model", text: stringBinding(key, "claude")).frame(minWidth: 120)
            .accessibilityIdentifier("situation-claude-\(key)")
        effortPicker("Claude effort", phase: key, field: "claude_reasoning")
        TextField("Gemini model", text: stringBinding(key, "gemini")).frame(minWidth: 120)
            .accessibilityIdentifier("situation-gemini-\(key)")
        effortPicker("Gemini effort", phase: key, field: "gemini_reasoning")
        TextField("Local model", text: stringBinding(key, "ollama")).frame(minWidth: 120)
            .accessibilityIdentifier("situation-ollama-\(key)")
        effortPicker("Local effort", phase: key, field: "ollama_reasoning")
        TextField("Cast ids", text: Binding(get: { casts.joined(separator: ", ") }, set: { text in
            mutate { SituationEditCodec.setCast(key, ids: text.split(separator: ",")
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }, in: &$0) }
        })).frame(minWidth: 130).accessibilityIdentifier("situation-cast-\(key)")
    }

    private func effortPicker(_ label: String, phase: String, field: String) -> some View {
        Picker(label, selection: stringBinding(phase, field)) {
            Text("Default").tag(""); Text("Low").tag("low"); Text("Medium").tag("medium")
            Text("High").tag("high"); Text("Max").tag("max")
        }.frame(minWidth: 110).accessibilityIdentifier("situation-\(field)-\(phase)")
    }

    private func sectionBinding(_ section: String) -> Binding<Bool> {
        Binding(get: {
            SituationEditCodec.overrides(canvas!)["sections"]?.object?[section]?.object?["enabled"]?.bool ?? true
        }, set: { enabled in mutate { SituationEditCodec.setSection(section, enabled: enabled, in: &$0) } })
    }

    private func stringBinding(_ phase: String, _ field: String) -> Binding<String> {
        Binding(get: {
            SituationEditCodec.overrides(canvas!)["phases"]?.object?[phase]?.object?[field]?.string ?? ""
        }, set: { text in mutate { SituationEditCodec.setPhaseField(phase, field: field,
            value: text.isEmpty ? nil : .string(text), in: &$0) } })
    }

    private func intBinding(_ phase: String, _ field: String) -> Binding<Int> {
        Binding(get: {
            SituationEditCodec.overrides(canvas!)["phases"]?.object?[phase]?.object?[field]?.int ?? 0
        }, set: { value in mutate { SituationEditCodec.setPhaseField(phase, field: field,
            value: value == 0 ? nil : .number(Double(value)), in: &$0) } })
    }

    private func reload(selecting requested: URL? = nil) {
        _ = store.ensureSituationSeeds()
        records = store.readSituationFiles()
        presetRecords = store.listPipelinePresets()
        switch store.readDocumentMap() { case .success(let value): map = value; case .failure(let error): note = error }
        let target = requested.flatMap { url in records.first { $0.url == url } }
            ?? selectedRecord ?? records.first
        if let target { select(target) }
    }

    private func select(_ record: SituationFileRecord) {
        selectedURL = record.url
        switch SituationCodec.decode(record.data, nameHint: record.name) {
        case .success(let value): canvas = value; note = record.error
        case .failure(let error): canvas = nil; note = error
        }
    }

    private func mutate(_ body: (inout SituationCanvas) -> Void) {
        guard var value = canvas else { return }; body(&value); canvas = value
    }

    private func save() {
        guard let canvas, let url = selectedURL else { return }
        do { _ = try store.writeSituation(canvas, to: url); reload(selecting: url); note = "Saved." }
        catch { note = error.localizedDescription }
    }

    private func duplicateSelected() {
        guard let canvas else { return }
        if let url = store.duplicateSituation(canvas) { reload(selecting: url) }
    }

    private func deleteSelected() {
        guard let record = selectedRecord else { return }
        store.deleteSituation(record); selectedURL = nil; canvas = nil; reload()
    }

    private func prepareApply(_ project: Project) {
        guard canvas != nil, let selectedRef else { return }
        switch store.engineSituationDiff(project: project, candidate: selectedRef) {
        case .success(let diff): applyDiff = diff; applyProject = project
        case .failure(let error): note = "Impact preview unavailable: \(error)"
        }
    }

    private func applySheet(_ project: Project) -> some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            Text("Apply to \(project.name)?").font(DS.font.title)
            if let diff = applyDiff {
                Text(diff.hasChanges ? "Review the exact engine-parity impact before writing." : "No changes — this Situation is already effective.")
                diffLine("Phases added", diff.phasesAdded)
                diffLine("Phases removed", diff.phasesRemoved)
                diffLine("Sections activated", diff.sectionsActivated)
                diffLine("Sections deactivated", diff.sectionsDeactivated)
                Text("Slot count delta: \(diff.slotDelta >= 0 ? "+" : "")\(diff.slotDelta)")
                    .font(DS.font.body)
            }
            Text("Confirm writes only run_config.json; the engine reads the ref at run start. Live adjustment depends on the engine's dynamic Situation lifecycle—the GUI never signals or restarts a process.")
                .font(DS.font.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { applyProject = nil }.accessibilityIdentifier("situation-apply-cancel")
                Button("Confirm") {
                    guard let selectedRef else { return }
                    do {
                        let wrote = try SituationApplyService.confirm(
                            situation: selectedRef,
                            runConfigURL: project.dirURL.appendingPathComponent("run_config.json"))
                        note = wrote ? "Applied to \(project.name)." : "No changes."
                        applyProject = nil
                    } catch { note = error.localizedDescription }
                }.buttonStyle(.borderedProminent).accessibilityIdentifier("situation-apply-confirm")
            }
        }.padding(DS.space.l).frame(width: 560)
    }

    private func diffLine(_ title: String, _ values: [String]) -> some View {
        Text("\(title): \(values.isEmpty ? "None" : values.joined(separator: ", "))")
            .font(DS.font.body)
    }
}
