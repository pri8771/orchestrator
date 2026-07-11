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
            Rectangle().fill(ThemeTokens.borderSubtle).frame(height: 1)
            HStack(spacing: 0) {
                workflowList
                    .frame(width: 280)
                Rectangle().fill(ThemeTokens.borderSubtle).frame(width: 1)
                if let file = selectedFile {
                    WorkflowEditorPane(file: file,
                                       onChanged: { keepSelection in reload(select: keepSelection) })
                        .id(file.name)
                        .environmentObject(store)
                } else {
                    Text("select a workflow")
                        .font(ThemeTokens.mono(12)).foregroundColor(ThemeTokens.dim)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .frame(width: 900, height: 640)
        .background(ThemeTokens.bg)
        .preferredColorScheme(.dark)
        .onAppear { reload(select: nil) }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text("workflows")
                .font(ThemeTokens.mono(14, .semibold)).foregroundColor(ThemeTokens.text)
            Text(store.workflowsDirURL.path)
                .font(ThemeTokens.mono(10.5)).foregroundColor(ThemeTokens.dim)
                .lineLimit(1).truncationMode(.head)
            Spacer()
            Text("esc to close")
                .font(ThemeTokens.mono(10.5)).foregroundColor(ThemeTokens.dim)
            Button { dismiss() } label: {
                Text("close")
                    .font(ThemeTokens.mono(11, .semibold)).foregroundColor(ThemeTokens.text)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .overlay(RoundedRectangle(cornerRadius: 6)
                        .stroke(ThemeTokens.borderStrong, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, 14)
        .frame(height: 44)
    }

    // MARK: Left list — every *.json in the engine's workflows/ dir

    private var workflowList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(files) { f in
                    workflowRow(f)
                }
                if files.isEmpty {
                    Text("no workflow files found")
                        .font(ThemeTokens.mono(10.5)).foregroundColor(ThemeTokens.dim)
                        .padding(8)
                }
            }
            .padding(10)
        }
    }

    private func workflowRow(_ f: RawWorkflowFile) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(f.name)
                        .font(ThemeTokens.mono(12.5, .semibold))
                        .foregroundColor(ThemeTokens.text)
                        .lineLimit(1)
                    if f.isBuiltIn {
                        Text("built-in")
                            .font(ThemeTokens.mono(9.5)).foregroundColor(ThemeTokens.dim)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .overlay(RoundedRectangle(cornerRadius: 4)
                                .stroke(ThemeTokens.borderStrong, lineWidth: 1))
                    }
                }
                Text("\(f.phaseCount) phases\(f.hasOverrides ? " · overrides" : "")")
                    .font(ThemeTokens.mono(10.5)).foregroundColor(ThemeTokens.dim)
            }
            Spacer()
            Button {
                if let newName = store.duplicateWorkflow(named: f.name) {
                    reload(select: newName)
                }
            } label: {
                Text("duplicate")
                    .font(ThemeTokens.mono(10, .semibold)).foregroundColor(ThemeTokens.accent)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .overlay(RoundedRectangle(cornerRadius: 5)
                        .stroke(ThemeTokens.accent.opacity(0.4), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .help("Clone \(f.name) to a new editable workflow")
            .accessibilityLabel("Duplicate \(f.name)")
        }
        .padding(.horizontal, 10).padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(selectedName == f.name ? ThemeTokens.card : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: 6)
            .stroke(selectedName == f.name ? ThemeTokens.accent.opacity(0.55)
                                           : ThemeTokens.borderSubtle, lineWidth: 1))
        .contentShape(Rectangle())
        .onTapGesture { selectedName = f.name }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(f.name), \(f.phaseCount) phases\(f.isBuiltIn ? ", built-in" : "")")
        .accessibilityAddTraits(selectedName == f.name ? .isSelected : [])
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
            Rectangle().fill(ThemeTokens.borderSubtle).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    titleSection
                    phasesSection
                    presetSection
                    warningsSection
                }
                .padding(16)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .confirmationDialog("delete workflow \"\(file.name)\"?",
                            isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("move to trash", role: .destructive) {
                store.deleteCustomWorkflow(named: file.name)
                onChanged(nil)
            }
            Button("cancel", role: .cancel) {}
        }
    }

    // MARK: header (name + save / delete)

    private var editorHeader: some View {
        HStack(spacing: 10) {
            Text(file.name)
                .font(ThemeTokens.mono(13, .semibold)).foregroundColor(ThemeTokens.text)
                .lineLimit(1)
            if isBuiltIn {
                Text("built-in · read-only — duplicate to edit")
                    .font(ThemeTokens.mono(10.5)).foregroundColor(ThemeTokens.dim)
            }
            if savedNote {
                Text("✓ saved")
                    .font(ThemeTokens.mono(10.5)).foregroundColor(ThemeTokens.ok)
            }
            Spacer()
            if !isBuiltIn {
                Button { confirmDelete = true } label: {
                    Text("✗ delete")
                        .font(ThemeTokens.mono(10.5, .semibold)).foregroundColor(ThemeTokens.fail)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .overlay(RoundedRectangle(cornerRadius: 5)
                            .stroke(ThemeTokens.fail.opacity(0.4), lineWidth: 1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Delete \(file.name)")
                Button { save() } label: {
                    Text("save")
                        .font(ThemeTokens.mono(11.5, .bold))
                        .foregroundColor(canSave ? ThemeTokens.accentOn : ThemeTokens.dim)
                        .padding(.horizontal, 14).padding(.vertical, 4)
                        .background(RoundedRectangle(cornerRadius: 6)
                            .fill(canSave ? ThemeTokens.accent : ThemeTokens.borderSubtle))
                }
                .buttonStyle(.plain)
                .disabled(!canSave)
                .keyboardShortcut("s", modifiers: .command)
                .accessibilityLabel("Save \(file.name)")
            }
        }
        .padding(.horizontal, 16)
        .frame(height: 44)
    }

    // MARK: title + description

    private var titleSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 5) {
                fieldLabel("TITLE")
                if isBuiltIn {
                    Text(title)
                        .font(ThemeTokens.mono(12)).foregroundColor(ThemeTokens.muted)
                } else {
                    editorTextField($title, accessibility: "Workflow title")
                }
            }
            VStack(alignment: .leading, spacing: 5) {
                fieldLabel("DESCRIPTION")
                if isBuiltIn {
                    Text(descriptionText.isEmpty ? "—" : descriptionText)
                        .font(ThemeTokens.mono(11)).foregroundColor(ThemeTokens.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    editorTextField($descriptionText, accessibility: "Workflow description")
                }
            }
        }
    }

    private func editorTextField(_ text: Binding<String>, accessibility: String) -> some View {
        TextField("", text: text)
            .textFieldStyle(.plain)
            .font(ThemeTokens.mono(12))
            .foregroundColor(ThemeTokens.text)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 6).fill(ThemeTokens.bg))
            .overlay(RoundedRectangle(cornerRadius: 6)
                .stroke(ThemeTokens.borderStrong, lineWidth: 1))
            .accessibilityLabel(accessibility)
    }

    // MARK: phases (toggle / drag-reorder / rounds)

    private var phasesSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                fieldLabel("PHASES")
                Text("\(includedCount)/\(phases.count) included · drag to reorder")
                    .font(ThemeTokens.mono(10)).foregroundColor(ThemeTokens.dim)
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
        return HStack(spacing: 8) {
            Text("⠿").font(ThemeTokens.mono(11)).foregroundColor(ThemeTokens.dim)
                .accessibilityHidden(true)
            Button { phase.wrappedValue.included.toggle() } label: {
                Text(p.included ? "✓" : "◌")
                    .font(ThemeTokens.mono(11, .bold))
                    .foregroundColor(p.included ? ThemeTokens.ok : ThemeTokens.dim)
                    .frame(width: 14)
            }
            .buttonStyle(.plain)
            .disabled(isBuiltIn)
            .accessibilityLabel("\(p.key) \(p.included ? "included" : "excluded")")
            Text(p.key)
                .font(ThemeTokens.mono(11.5, .medium))
                .foregroundColor(p.included ? ThemeTokens.text : ThemeTokens.dim)
                .lineLimit(1)
            if p.writes {
                Text("writes")
                    .font(ThemeTokens.mono(9.5)).foregroundColor(ThemeTokens.accent.opacity(0.8))
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .overlay(RoundedRectangle(cornerRadius: 4)
                        .stroke(ThemeTokens.accent.opacity(0.35), lineWidth: 1))
            }
            Spacer()
            Text("rounds")
                .font(ThemeTokens.mono(9.5)).foregroundColor(ThemeTokens.dim)
            HStack(spacing: 7) {
                Button("−") { phase.wrappedValue.rounds = max(1, p.rounds - 1) }
                    .accessibilityLabel("Fewer rounds for \(p.key)")
                Text("\(p.rounds)").frame(minWidth: 12)
                Button("+") { phase.wrappedValue.rounds = min(9, p.rounds + 1) }
                    .accessibilityLabel("More rounds for \(p.key)")
            }
            .buttonStyle(.plain)
            .font(ThemeTokens.mono(11, .semibold))
            .foregroundColor(p.included && !isBuiltIn ? ThemeTokens.text : ThemeTokens.dim)
            .padding(.vertical, 2).padding(.horizontal, 7)
            .overlay(RoundedRectangle(cornerRadius: 5)
                .stroke(ThemeTokens.borderStrong, lineWidth: 1))
            .disabled(isBuiltIn || !p.included)
        }
        .padding(.horizontal, 9).padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(p.included ? ThemeTokens.card : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: 6)
            .stroke(ThemeTokens.borderSubtle, lineWidth: 1))
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
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                fieldLabel("PRESET")
                Text("saved as top-level \"overrides\" · keep-default keys omitted")
                    .font(ThemeTokens.mono(10)).foregroundColor(ThemeTokens.dim)
            }
            HStack(alignment: .top, spacing: 14) {
                modelMenu(label: "claude model", selection: $claudeModel, options: claudeOptions)
                modelMenu(label: "codex model", selection: $codexModel, options: codexOptions)
            }
            HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("effort")
                        .font(ThemeTokens.mono(10)).foregroundColor(ThemeTokens.muted)
                    effortSegments
                }
                VStack(alignment: .leading, spacing: 5) {
                    Text("rounds ×")
                        .font(ThemeTokens.mono(10)).foregroundColor(ThemeTokens.muted)
                    HStack(spacing: 10) {
                        Slider(value: $roundsScale, in: 0.5...2.0, step: 0.1)
                            .tint(ThemeTokens.accent)
                            .frame(width: 180)
                            .disabled(isBuiltIn)
                            .accessibilityLabel("Rounds scale")
                        Text(String(format: "%.1f×", roundsScale))
                            .font(ThemeTokens.mono(11, .semibold))
                            .foregroundColor(abs(roundsScale - 1.0) < 0.001
                                             ? ThemeTokens.muted : ThemeTokens.text)
                    }
                }
            }
        }
    }

    private func modelMenu(label: String, selection: Binding<String>,
                           options: [String]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(ThemeTokens.mono(10)).foregroundColor(ThemeTokens.muted)
            Menu {
                Button("keep default") { selection.wrappedValue = "" }
                Divider()
                ForEach(options, id: \.self) { m in
                    Button(m) { selection.wrappedValue = m }
                }
            } label: {
                HStack(spacing: 6) {
                    Text(selection.wrappedValue.isEmpty ? "keep default" : selection.wrappedValue)
                        .font(ThemeTokens.mono(11))
                        .foregroundColor(selection.wrappedValue.isEmpty
                                         ? ThemeTokens.muted : ThemeTokens.text)
                        .lineLimit(1)
                    Text("▾").font(ThemeTokens.mono(9)).foregroundColor(ThemeTokens.dim)
                }
                .padding(.horizontal, 9).padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: 6).fill(ThemeTokens.bg))
                .overlay(RoundedRectangle(cornerRadius: 6)
                    .stroke(ThemeTokens.borderStrong, lineWidth: 1))
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .disabled(isBuiltIn)
            .accessibilityLabel(label)
        }
    }

    private var effortSegments: some View {
        HStack(spacing: 2) {
            ForEach(["fast", "standard", "max"], id: \.self) { e in
                Button { effort = e } label: {
                    Text(e)
                        .font(ThemeTokens.mono(11, effort == e ? .bold : .regular))
                        .foregroundColor(effort == e ? ThemeTokens.accentOn : ThemeTokens.muted)
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background(RoundedRectangle(cornerRadius: 5)
                            .fill(effort == e ? ThemeTokens.accent : Color.clear))
                }
                .buttonStyle(.plain)
                .disabled(isBuiltIn)
                .accessibilityLabel("Effort \(e)")
                .accessibilityAddTraits(effort == e ? .isSelected : [])
            }
        }
        .padding(3)
        .background(RoundedRectangle(cornerRadius: 6).fill(ThemeTokens.bg))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(ThemeTokens.borderStrong, lineWidth: 1))
    }

    // MARK: validation warnings

    @ViewBuilder
    private var warningsSection: some View {
        if includedCount == 0 {
            Text("✗ include at least one phase to save")
                .font(ThemeTokens.mono(11)).foregroundColor(ThemeTokens.fail)
        }
        if includedHasWrites && resultingBuildPhaseIsNull {
            Text("✗ a writes:true phase is included but build_phase is null — the build step won't run")
                .font(ThemeTokens.mono(11)).foregroundColor(ThemeTokens.fail)
        }
    }

    private func fieldLabel(_ s: String) -> some View {
        Text(s)
            .font(ThemeTokens.mono(10, .semibold))
            .foregroundColor(ThemeTokens.muted)
            .kerning(0.5)
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
