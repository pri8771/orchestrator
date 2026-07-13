import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - Workflow builder (the dashboard "workflows" sheet)
//
// Author custom workflow pipelines without leaving the GUI: duplicate a
// built-in, toggle/reorder its phases, tune per-phase rounds, and pin preset
// overrides (models / effort / rounds scale). Built-ins are read-only —
// --seed re-materializes them, so edits would be clobbered.
//
// Restyled onto the DS ramp (DESIGN-REFRESH.md tranche 2): light+dark native,
// SF Pro labels, mono only for phase keys and model IDs. The legacy
// GitHub-dark lime skin is gone.
//
// Files are edited as RAW JSON dictionaries (JSONSerialization, never a
// Codable round-trip): every phase's ORIGINAL dict is carried through
// untouched when phases are toggled/reordered, so fields the GUI doesn't
// model — verify specs, budgets, checkpoints, doc_sections — survive a save.

struct WorkflowBuilderSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss

    @State private var files: [RawWorkflowFile] = []
    @State private var selectedName: String?

    private var selectedFile: RawWorkflowFile? {
        files.first { $0.name == selectedName }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                workflowList
                    .frame(width: 280)
                Divider()
                if let file = selectedFile {
                    WorkflowEditorPane(file: file,
                                       onChanged: { keepSelection in reload(select: keepSelection) })
                        .id(file.name)
                        .environmentObject(store)
                } else {
                    EmptyStateView(symbol: "arrow.triangle.branch",
                                   title: "Select a workflow",
                                   message: "Pick a workflow on the left to view or edit its phases and preset.")
                }
            }
        }
        .frame(width: 900, height: 640)
        .background(DS.windowBg)
        .onAppear { reload(select: nil) }
    }

    private var header: some View {
        HStack(spacing: DS.space.xs) {
            Text("Workflows")
                .font(DS.font.headline)
            Text(store.workflowsDirURL.path)
                .font(DS.font.monoInline).foregroundStyle(.tertiary)
                .lineLimit(1).truncationMode(.head)
            Spacer()
            Button("Close") { dismiss() }
                .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, DS.space.m)
        .frame(height: 44)
    }

    // MARK: Left list — every *.json in the engine's workflows/ dir

    private var workflowList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.xxs) {
                ForEach(files) { f in
                    workflowRow(f)
                }
                if files.isEmpty {
                    Text("No workflow files found.")
                        .font(DS.font.caption).foregroundStyle(.tertiary)
                        .padding(DS.space.xs)
                }
            }
            .padding(DS.space.xs)
        }
    }

    private func workflowRow(_ f: RawWorkflowFile) -> some View {
        let selected = selectedName == f.name
        return HStack(spacing: DS.space.xs) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: DS.space.xxs) {
                    Text(f.name)
                        .font(DS.font.body.weight(.medium))
                        .lineLimit(1)
                    if f.isBuiltIn {
                        Text("built-in")
                            .font(DS.font.caption2)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .overlay(RoundedRectangle(cornerRadius: DS.radius.control - 2,
                                                      style: .continuous)
                                .stroke(DS.hairline, lineWidth: 1))
                    }
                }
                Text("\(f.phaseCount) phases\(f.hasOverrides ? " · overrides" : "")")
                    .font(DS.font.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Duplicate") {
                if let newName = store.duplicateWorkflow(named: f.name) {
                    reload(select: newName)
                }
            }
            .controlSize(.small)
            .help("Clone \(f.name) to a new editable workflow")
            .accessibilityLabel("Duplicate \(f.name)")
        }
        .padding(.horizontal, DS.space.xs).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
            .fill(selected ? AnyShapeStyle(DS.accent.fill) : AnyShapeStyle(Color.clear)))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
            .stroke(selected ? DS.accent.stroke : Color.clear, lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { selectedName = f.name }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(f.name), \(f.phaseCount) phases\(f.isBuiltIn ? ", built-in" : "")")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func reload(select: String?) {
        files = store.readRawWorkflows().map(RawWorkflowFile.init)
        if let select { selectedName = select }
        if selectedName == nil || !files.contains(where: { $0.name == selectedName }) {
            selectedName = files.first?.name
        }
    }
}

// One workflow file, kept as its raw top-level dictionary.
struct RawWorkflowFile: Identifiable {
    let fileURL: URL
    let obj: [String: Any]

    init(fileURL: URL, obj: [String: Any]) {
        self.fileURL = fileURL
        self.obj = obj
    }

    var name: String {
        (obj["name"] as? String) ?? fileURL.deletingPathExtension().lastPathComponent
    }
    var phaseCount: Int { (obj["phases"] as? [[String: Any]])?.count ?? 0 }
    var hasOverrides: Bool { !((obj["overrides"] as? [String: Any]) ?? [:]).isEmpty }
    var isBuiltIn: Bool { OrchestratorStore.builtInWorkflowNames.contains(name) }
    var id: String { name }
}

// MARK: - Right editor pane (state resets via .id(file.name) on selection change)

private struct WorkflowEditorPane: View {
    @EnvironmentObject var store: OrchestratorStore
    let file: RawWorkflowFile
    // Called after any file mutation (save / delete); the argument is the
    // workflow the list should select afterwards (nil = first).
    let onChanged: (String?) -> Void

    @State private var title: String
    @State private var descriptionText: String
    @State private var phases: [EditablePhase]
    @State private var claudeModel: String   // "" = keep default
    @State private var codexModel: String    // "" = keep default
    @State private var effort: String        // fast / standard / max
    @State private var roundsScale: Double   // 0.5 – 2.0, 1.0 = keep default
    @State private var draggingPhase: String?
    @State private var confirmDelete = false
    @State private var savedNote = false

    private var isBuiltIn: Bool { file.isBuiltIn }

    init(file: RawWorkflowFile, onChanged: @escaping (String?) -> Void) {
        self.file = file
        self.onChanged = onChanged
        _title = State(initialValue: (file.obj["title"] as? String) ?? file.name)
        _descriptionText = State(initialValue: (file.obj["description"] as? String) ?? "")
        let raw = (file.obj["phases"] as? [[String: Any]]) ?? []
        _phases = State(initialValue: raw.enumerated().map { idx, dict in
            EditablePhase(key: (dict["key"] as? String) ?? "phase\(idx + 1)", dict: dict)
        })
        let ov = (file.obj["overrides"] as? [String: Any]) ?? [:]
        _claudeModel = State(initialValue: (ov["claude_model"] as? String) ?? "")
        _codexModel = State(initialValue: (ov["codex_model"] as? String) ?? "")
        _effort = State(initialValue: (ov["effort"] as? String) ?? "standard")
        let scale = (ov["rounds_scale"] as? Double)
            ?? (ov["rounds_scale"] as? Int).map(Double.init) ?? 1.0
        _roundsScale = State(initialValue: min(2.0, max(0.5, scale)))
    }

    // MARK: derived validation

    private var includedCount: Int { phases.filter(\.included).count }
    private var includedHasWrites: Bool { phases.contains { $0.included && $0.writes } }
    // The build_phase the saved file would end up with: null when every
    // writes:true phase is excluded, else whatever the file already says.
    private var resultingBuildPhaseIsNull: Bool {
        if !includedHasWrites { return true }
        let bp = file.obj["build_phase"]
        return bp == nil || bp is NSNull || ((bp as? String)?.isEmpty ?? false)
    }
    private var canSave: Bool { !isBuiltIn && includedCount >= 1 }

    var body: some View {
        VStack(spacing: 0) {
            editorHeader
            Divider()
            warningsSection
            ScrollView {
                VStack(alignment: .leading, spacing: DS.space.m) {
                    titleSection
                    phasesSection
                    presetSection
                }
                .padding(DS.space.m)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .confirmationDialog("Delete workflow \"\(file.name)\"?",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Move to Trash", role: .destructive) {
                store.deleteCustomWorkflow(named: file.name)
                onChanged(nil)
            }
            Button("Cancel", role: .cancel) {}
        }
    }

    // MARK: header (name + save / delete)

    private var editorHeader: some View {
        HStack(spacing: DS.space.xs) {
            Text(file.name)
                .font(DS.font.headline)
                .lineLimit(1)
            if isBuiltIn {
                Text("built-in · read-only — duplicate to edit")
                    .font(DS.font.caption).foregroundStyle(.secondary)
            }
            if savedNote {
                Label("Saved", systemImage: "checkmark")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.success.color)
            }
            Spacer()
            if !isBuiltIn {
                Button("Delete…", role: .destructive) { confirmDelete = true }
                    .accessibilityLabel("Delete \(file.name)")
                Button("Save") { save() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canSave)
                    .keyboardShortcut("s", modifiers: .command)
                    .accessibilityLabel("Save \(file.name)")
            }
        }
        .padding(.horizontal, DS.space.m)
        .frame(height: 44)
    }

    // MARK: title + description

    private var titleSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            VStack(alignment: .leading, spacing: DS.space.xxs) {
                fieldLabel("Title")
                if isBuiltIn {
                    Text(title)
                        .font(DS.font.body).foregroundStyle(.secondary)
                } else {
                    TextField("Title", text: $title)
                        .textFieldStyle(.roundedBorder)
                        .font(DS.font.body)
                        .accessibilityLabel("Workflow title")
                }
            }
            VStack(alignment: .leading, spacing: DS.space.xxs) {
                fieldLabel("Description")
                if isBuiltIn {
                    Text(descriptionText.isEmpty ? "—" : descriptionText)
                        .font(DS.font.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    TextField("Description", text: $descriptionText)
                        .textFieldStyle(.roundedBorder)
                        .font(DS.font.body)
                        .accessibilityLabel("Workflow description")
                }
            }
        }
    }

    // MARK: phases (toggle / drag-reorder / rounds)

    private var phasesSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xs) {
                fieldLabel("Phases")
                Text("\(includedCount)/\(phases.count) included · drag to reorder")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
            ForEach($phases) { $phase in
                phaseRow($phase)
                    .onDrag {
                        guard !isBuiltIn else { return NSItemProvider() }
                        draggingPhase = phase.key
                        return NSItemProvider(object: phase.key as NSString)
                    }
                    .onDrop(of: [UTType.text],
                            delegate: PhaseReorderDelegate(item: phase.key,
                                                           dragging: $draggingPhase,
                                                           phases: $phases))
            }
        }
    }

    private func phaseRow(_ phase: Binding<EditablePhase>) -> some View {
        let p = phase.wrappedValue
        return HStack(spacing: DS.space.xs) {
            Image(systemName: "line.3.horizontal")
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .accessibilityHidden(true)
            Button { phase.wrappedValue.included.toggle() } label: {
                Image(systemName: p.included ? "checkmark.circle.fill" : "circle")
                    .font(DS.font.body)
                    .foregroundStyle(p.included ? DS.status.success.color : DS.textTertiary)
                    .frame(width: 16)
            }
            .buttonStyle(.plain)
            .disabled(isBuiltIn)
            .accessibilityLabel("\(p.key) \(p.included ? "included" : "excluded")")
            // Phase keys are machine text — the one mono rendering here.
            Text(p.key)
                .font(DS.font.monoInline)
                .foregroundStyle(p.included ? DS.textPrimary : DS.textSecondary)
                .lineLimit(1)
            if p.writes {
                Text("writes")
                    .font(DS.font.caption2)
                    .foregroundStyle(DS.accent.color)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Capsule().fill(DS.accent.fill))
                    .overlay(Capsule().stroke(DS.accent.stroke, lineWidth: 1))
            }
            Spacer()
            Text("rounds")
                .font(DS.font.caption).foregroundStyle(.secondary)
            HStack(spacing: DS.space.xs) {
                Button("−") { phase.wrappedValue.rounds = max(1, p.rounds - 1) }
                    .accessibilityLabel("Fewer rounds for \(p.key)")
                Text("\(p.rounds)").monospacedDigit().frame(minWidth: 12)
                Button("+") { phase.wrappedValue.rounds = min(9, p.rounds + 1) }
                    .accessibilityLabel("More rounds for \(p.key)")
            }
            .buttonStyle(.plain)
            .font(DS.font.callout)
            .foregroundStyle(p.included && !isBuiltIn ? DS.textPrimary : DS.textSecondary)
            .padding(.vertical, 2).padding(.horizontal, DS.space.xs)
            .overlay(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                .stroke(DS.hairline, lineWidth: 1))
            .disabled(isBuiltIn || !p.included)
        }
        .padding(.horizontal, DS.space.xs).padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .fill(.quaternary.opacity(p.included ? 0.5 : 0.15)))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .stroke(DS.hairline, lineWidth: 1))
        .opacity(p.included ? 1 : 0.7)
        .contentShape(Rectangle())
    }

    // MARK: preset overrides

    // Model menus: keep-default + the curated ids the task pins + everything
    // Configuration.swift's pickers already know for that provider.
    private var claudeOptions: [String] {
        dedupe(["claude-sonnet-5", "claude-haiku-4-5"] + store.modelPresets("claude"))
    }
    private var codexOptions: [String] {
        dedupe(["gpt-5.3-codex-spark"] + store.modelPresets("codex"))
    }

    private func dedupe(_ xs: [String]) -> [String] {
        var seen = Set<String>(); var out: [String] = []
        for x in xs where !x.isEmpty && seen.insert(x).inserted { out.append(x) }
        return out
    }

    private var presetSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(spacing: DS.space.xs) {
                fieldLabel("Preset")
                Text("saved as top-level \"overrides\" · keep-default keys omitted")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
            HStack(alignment: .top, spacing: DS.space.m) {
                modelMenu(label: "Claude model", selection: $claudeModel, options: claudeOptions)
                modelMenu(label: "Codex model", selection: $codexModel, options: codexOptions)
            }
            HStack(alignment: .top, spacing: DS.space.m) {
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Effort")
                        .font(DS.font.caption).foregroundStyle(.secondary)
                    Picker("", selection: $effort) {
                        ForEach(["fast", "standard", "max"], id: \.self) { e in
                            Text(e.capitalized).tag(e)
                        }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .frame(width: 220)
                    .disabled(isBuiltIn)
                    .accessibilityLabel("Effort")
                }
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Rounds ×")
                        .font(DS.font.caption).foregroundStyle(.secondary)
                    HStack(spacing: DS.space.xs) {
                        Slider(value: $roundsScale, in: 0.5...2.0, step: 0.1)
                            .frame(width: 180)
                            .disabled(isBuiltIn)
                            .accessibilityLabel("Rounds scale")
                        Text(String(format: "%.1f×", roundsScale))
                            .font(DS.font.monoInline)
                            .foregroundStyle(abs(roundsScale - 1.0) < 0.001
                                             ? DS.textSecondary : DS.textPrimary)
                    }
                }
            }
        }
    }

    private func modelMenu(label: String, selection: Binding<String>,
                           options: [String]) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text(label)
                .font(DS.font.caption).foregroundStyle(.secondary)
            Menu {
                Button("Keep default") { selection.wrappedValue = "" }
                Divider()
                ForEach(options, id: \.self) { m in
                    Button(m) { selection.wrappedValue = m }
                }
            } label: {
                HStack(spacing: DS.space.xxs) {
                    // Model IDs are machine text (mono); "keep default" is prose.
                    Text(selection.wrappedValue.isEmpty ? "Keep default" : selection.wrappedValue)
                        .font(selection.wrappedValue.isEmpty ? DS.font.callout : DS.font.monoInline)
                        .foregroundStyle(selection.wrappedValue.isEmpty
                                         ? DS.textSecondary : DS.textPrimary)
                        .lineLimit(1)
                    Image(systemName: "chevron.up.chevron.down")
                        .font(DS.font.caption2).foregroundStyle(.tertiary)
                }
                .padding(.horizontal, DS.space.xs).padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                    .fill(DS.cardBg))
                .overlay(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                    .stroke(DS.hairline, lineWidth: 1))
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .disabled(isBuiltIn)
            .accessibilityLabel(label)
        }
    }

    // MARK: validation warnings (InlineBanner — the one banner)

    @ViewBuilder
    private var warningsSection: some View {
        if includedCount == 0 {
            InlineBanner(kind: .error, title: "Include at least one phase to save")
        }
        if includedHasWrites && resultingBuildPhaseIsNull {
            InlineBanner(kind: .error, title: "Build step won't run",
                         message: "A writes:true phase is included but build_phase is null.")
        }
    }

    private func fieldLabel(_ s: String) -> some View {
        Text(s)
            .font(DS.font.callout)
            .foregroundStyle(.secondary)
    }

    // MARK: save — filter/reorder the ORIGINAL phase dicts, update rounds,
    // write presets as top-level "overrides" (keep-default keys omitted).

    private func save() {
        guard canSave else { return }
        var obj = file.obj
        obj["title"] = title
        obj["description"] = descriptionText
        obj["phases"] = phases.filter(\.included).map { p -> [String: Any] in
            var d = p.dict
            d["rounds"] = p.rounds
            return d
        }
        var ov: [String: Any] = [:]
        if !claudeModel.isEmpty { ov["claude_model"] = claudeModel }
        if !codexModel.isEmpty { ov["codex_model"] = codexModel }
        if effort != "standard" { ov["effort"] = effort }
        if abs(roundsScale - 1.0) > 0.001 {
            ov["rounds_scale"] = (roundsScale * 10).rounded() / 10
        }
        if ov.isEmpty { obj.removeValue(forKey: "overrides") } else { obj["overrides"] = ov }
        if !includedHasWrites { obj["build_phase"] = NSNull() }
        guard store.saveCustomWorkflow(obj, named: file.name) else { return }
        savedNote = true
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            savedNote = false
        }
        onChanged(file.name)
    }
}

// One phase in the editor: the ORIGINAL raw dict (carried through a save
// untouched, except "rounds"), plus the editable include flag and round count.
private struct EditablePhase: Identifiable {
    let key: String
    var dict: [String: Any]
    var included = true
    var rounds: Int

    init(key: String, dict: [String: Any]) {
        self.key = key
        self.dict = dict
        self.rounds = min(9, max(1, (dict["rounds"] as? Int) ?? 9))
    }

    var writes: Bool { (dict["writes"] as? Bool) ?? false }
    var id: String { key }
}

// Reorders the phase checklist as the drag passes over each row (same pattern
// as the dashboard's QueueReorderDelegate).
private struct PhaseReorderDelegate: DropDelegate {
    let item: String
    @Binding var dragging: String?
    @Binding var phases: [EditablePhase]

    func dropEntered(info: DropInfo) {
        guard let dragging, dragging != item,
              let from = phases.firstIndex(where: { $0.key == dragging }),
              let to = phases.firstIndex(where: { $0.key == item }) else { return }
        withAnimation(.easeInOut(duration: 0.15)) {
            phases.move(fromOffsets: IndexSet(integer: from),
                        toOffset: to > from ? to + 1 : to)
        }
    }
    func dropUpdated(info: DropInfo) -> DropProposal? { DropProposal(operation: .move) }
    func performDrop(info: DropInfo) -> Bool {
        dragging = nil
        return true
    }
}
