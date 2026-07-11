import Foundation
import SwiftUI

// One provider's enable toggle + model picker. Custom entries stay hidden behind
// a plus button so the normal settings render path is picker-only.
struct AgentModelRow: View {
    @EnvironmentObject var store: OrchestratorStore
    let agent: String
    @State private var addingCustomModel = false
    @State private var customModelID = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Toggle(isOn: Binding(
                    get: { store.enabledAgents[agent] ?? false },
                    set: { store.setAgentEnabled(agent, $0) }
                )) {
                    Text((Speaker(rawValue: agent) ?? .system).display).fontWeight(.medium)
                }
                .toggleStyle(.switch)
                Spacer()
                if store.cliAvailable[agent] ?? false {
                    Image(systemName: "checkmark.seal.fill").foregroundStyle(.green).font(.system(size: 11))
                } else {
                    Text("CLI not found").font(.system(size: 10)).foregroundStyle(.orange)
                }
            }
            HStack(spacing: 6) {
                Text("Model").font(.caption).foregroundStyle(.secondary).frame(width: 44, alignment: .leading)
                Picker("", selection: Binding(
                    get: { store.agentModels[agent] ?? store.defaultModel(agent) },
                    set: { store.setModel(agent, $0) }
                )) {
                    ForEach(store.modelPresets(agent), id: \.self) { preset in
                        Text(preset).tag(preset)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .accessibilityLabel("Model for \((Speaker(rawValue: agent) ?? .system).display)")
                Button {
                    addingCustomModel.toggle()
                } label: {
                    Image(systemName: addingCustomModel ? "minus.circle" : "plus.circle")
                }
                .buttonStyle(.plain)
                .help("Add a custom model option for this provider")
                .accessibilityLabel("Add custom model for \((Speaker(rawValue: agent) ?? .system).display)")
                Spacer()
            }
            if addingCustomModel {
                HStack(spacing: 6) {
                    Text("Add").font(.caption).foregroundStyle(.secondary).frame(width: 44, alignment: .leading)
                    TextField("model id", text: $customModelID)
                        .textFieldStyle(.roundedBorder)
                    Button("Add") {
                        if store.addModelPreset(agent: agent, id: customModelID) {
                            customModelID = ""
                            addingCustomModel = false
                        }
                    }
                    .disabled(!store.modelIDIsSafe(customModelID))
                }
                .font(.caption)
            }
            if store.effortKey(agent) != nil {
                HStack(spacing: 6) {
                    Text("Effort").font(.caption).foregroundStyle(.secondary).frame(width: 44, alignment: .leading)
                    Picker("", selection: Binding(
                        get: { store.agentEfforts[agent] ?? "low" },
                        set: { store.setReasoningEffort(agent, $0) }
                    )) {
                        ForEach(store.effortOptions, id: \.self) { Text($0.capitalized).tag($0) }
                    }
                    .labelsHidden().pickerStyle(.segmented).font(.subheadline)
                    .accessibilityLabel("Reasoning effort for \((Speaker(rawValue: agent) ?? .system).display)")
                    Spacer()
                }
            }
            HStack(spacing: 6) {
                Text("Role").font(.caption).foregroundStyle(.secondary).frame(width: 44, alignment: .leading)
                Picker("", selection: Binding(
                    get: { store.agentRoleOverrides[agent] ?? "" },
                    set: { store.setAgentRole(agent, $0) }
                )) {
                    Text("Phase default").tag("")
                    ForEach(store.roles) { role in
                        Text(role.name).tag(role.id)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .font(.subheadline)
                .accessibilityLabel("Preferred role for \((Speaker(rawValue: agent) ?? .system).display)")
                Spacer()
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Sub-agents: roles + rotating personalities

struct SubAgentsEditor: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss

    @State private var roles: [RoleDef] = []
    @State private var personalities: [PersonalityDef] = []
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Sub-agents").font(.title3).fontWeight(.medium)
                    Text("Roles are the lenses agents argue from. Personalities rotate every phase, so no agent is stuck to one voice.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
            .padding(.bottom, 12)

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    editorSection(
                        title: "Roles",
                        addLabel: "Add role",
                        onAdd: { roles.append(RoleDef(id: "role\(roles.count + 1)",
                                                      name: "New Role", focus: "what this role cares about")) }
                    ) {
                        ForEach($roles) { $role in
                            rowCard(onDelete: { roles.removeAll { $0.id == role.id } }) {
                                TextField("Name", text: $role.name).textFieldStyle(.roundedBorder)
                                TextField("Focus — what this role cares about", text: $role.focus)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 11))
                            }
                        }
                    }

                    editorSection(
                        title: "Personalities (rotate per phase)",
                        addLabel: "Add personality",
                        onAdd: { personalities.append(PersonalityDef(id: "p\(personalities.count + 1)",
                                                                     name: "the New One", style: "how this voice argues")) }
                    ) {
                        ForEach($personalities) { $p in
                            rowCard(onDelete: { personalities.removeAll { $0.id == p.id } }) {
                                TextField("Name (e.g. the Skeptic)", text: $p.name).textFieldStyle(.roundedBorder)
                                TextField("Style — how this voice argues", text: $p.style)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 11))
                            }
                        }
                    }
                }
            }
            .frame(maxHeight: 380)

            Divider().padding(.vertical, 10)
            HStack {
                Text("\(roles.count) roles · \(personalities.count) personalities")
                    .font(.caption2).foregroundStyle(.tertiary)
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Save") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(roles.isEmpty || personalities.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 520, height: 560)
        .onAppear {
            if !loaded { roles = store.roles; personalities = store.personalities; loaded = true }
        }
    }

    private func save() {
        // Ensure non-empty, unique ids (derive from the name when missing).
        var seen = Set<String>()
        func fix(_ id: String, _ name: String, _ i: Int) -> String {
            var base = id.trimmingCharacters(in: .whitespaces)
            if base.isEmpty { base = OrchestratorStore.slugify(name) }
            if base.isEmpty { base = "item\(i)" }
            var candidate = base; var n = 2
            while seen.contains(candidate) { candidate = "\(base)-\(n)"; n += 1 }
            seen.insert(candidate); return candidate
        }
        func nm(_ s: String, _ fallback: String, _ i: Int) -> String {
            let t = s.trimmingCharacters(in: .whitespaces)
            return t.isEmpty ? "\(fallback) \(i + 1)" : t
        }
        let rs = roles.enumerated().map { e in
            RoleDef(id: fix(e.element.id, e.element.name, e.offset),
                    name: nm(e.element.name, "Role", e.offset), focus: e.element.focus)
        }
        seen.removeAll()
        let ps = personalities.enumerated().map { e in
            PersonalityDef(id: fix(e.element.id, e.element.name, e.offset),
                           name: nm(e.element.name, "the Voice", e.offset), style: e.element.style)
        }
        store.saveRoles(rs, ps)
        dismiss()
    }

    // MARK: small building blocks

    @ViewBuilder
    private func editorSection<Content: View>(title: String, addLabel: String,
                                              onAdd: @escaping () -> Void,
                                              @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(title).font(.system(size: 13, weight: .medium))
                Spacer()
                Button(action: onAdd) { Label(addLabel, systemImage: "plus") }
                    .font(.caption)
            }
            content()
        }
    }

    @ViewBuilder
    private func rowCard<Content: View>(onDelete: @escaping () -> Void,
                                        @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 4) { content() }
            Button(action: onDelete) { Image(systemName: "trash").font(.system(size: 11)) }
                .buttonStyle(.plain).foregroundStyle(.secondary)
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.06)))
    }
}

// MARK: - Rounds & phases per workflow

struct RoundsEditor: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss

    @State private var selected = "app_build"
    @State private var rounds: [String: Int] = [:]

    private var wf: WorkflowDef? { store.workflow(named: selected) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Rounds & phases").font(.title3).fontWeight(.medium)
            Text("Set a maximum round count for each phase, or choose unlimited so discussion continues until consensus is accepted. 0/∞/unlimited are saved as no cap.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Picker("Workflow", selection: $selected) {
                ForEach(store.workflows) { w in Text(w.title).tag(w.name) }
            }
            .pickerStyle(.menu)
            .onChange(of: selected) { _ in loadRounds() }

            if let wf = wf, !wf.description.isEmpty {
                Text(wf.description).font(.system(size: 11)).foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            ScrollView {
                VStack(spacing: 6) {
                    ForEach(wf?.phases ?? []) { phase in
                        HStack(spacing: 10) {
                            Image(systemName: phase.writes ? "hammer" : "circle")
                                .font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 14)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(phase.title).font(.system(size: 12, weight: .medium))
                                if !phase.roles.isEmpty {
                                    Text(phase.roles.joined(separator: " · "))
                                        .font(.system(size: 10)).foregroundStyle(.tertiary)
                                }
                            }
                            Spacer()
                            HStack(spacing: 6) {
                                Toggle("", isOn: Binding(
                                    get: { (rounds[phase.key] ?? phase.rounds) == 0 },
                                    set: { isUnlimited in
                                        rounds[phase.key] = isUnlimited
                                            ? 0
                                            : max(1, rounds[phase.key] ?? phase.rounds)
                                    }
                                ))
                                .toggleStyle(.checkbox)
                                .help("Enable unlimited rounds and keep debating until consensus.")

                                if (rounds[phase.key] ?? phase.rounds) == 0 {
                                    Text("∞")
                                        .font(.system(size: 12, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 82, alignment: .trailing)
                                } else {
                                    TextField("", text: Binding(
                                        get: {
                                            let value = rounds[phase.key] ?? phase.rounds
                                            return "\(value)"
                                        },
                                        set: { raw in
                                            let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
                                            if isUnlimitedRoundsText(value) {
                                                rounds[phase.key] = 0
                                            } else if let n = Int(value) {
                                                rounds[phase.key] = min(120, max(1, n))
                                            }
                                        }
                                    ))
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 12, design: .monospaced))
                                    .multilineTextAlignment(.trailing)
                                    .frame(width: 82)
                                    .help("Enter 1–120, 0, ∞, or infinite")
                                    Stepper("", value: Binding(
                                        get: { max(1, rounds[phase.key] ?? phase.rounds) },
                                        set: { rounds[phase.key] = max(1, $0) }
                                    ), in: 1...120)
                                    .labelsHidden()
                                    .frame(width: 42)
                                    }
                            }
                        }
                        .padding(.vertical, 4)
                        Divider()
                    }
                }
            }
            .frame(maxHeight: 320)

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Save") { save() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 460, height: 520)
        .onAppear { loadRounds() }
    }

    private func loadRounds() {
        var r: [String: Int] = [:]
        for p in wf?.phases ?? [] { r[p.key] = p.rounds }
        rounds = r
    }

    private func save() {
        guard let wf = wf else { return }
        for p in wf.phases {
            let v = rounds[p.key] ?? p.rounds
            if v != p.rounds { store.setPhaseRounds(workflow: wf.name, phaseKey: p.key, rounds: v) }
        }
        dismiss()
    }

    private func isUnlimitedRoundsText(_ value: String) -> Bool {
        let normalized = value.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        return normalized == "0" || normalized == "∞" || normalized == "infinite"
            || normalized == "unlimited" || normalized == "inf" || normalized == "u"
    }
}

// MARK: - Settings scene (Cmd+,) — Native Pro M2: 7 tabs shrink to 4.
// General · Engine (paths, heartbeats, escalation) · Defaults ("Defaults for
// new projects": agent team + routing + ladders + gates) · Advanced.

struct SettingsView: View {
    @EnvironmentObject var store: OrchestratorStore

    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            EngineSettings().tabItem { Label("Engine", systemImage: "gauge") }
            DefaultsSettings().tabItem { Label("Defaults", systemImage: "slider.horizontal.3") }
            AdvancedSettings().tabItem { Label("Advanced", systemImage: "wrench.and.screwdriver") }
        }
        .frame(width: 780, height: 640)
    }
}

struct GeneralSettings: View {
    @EnvironmentObject var store: OrchestratorStore
    @AppStorage("defaultAutonomy") private var defaultAutonomy = "fully_autonomous"
    @AppStorage("defaultCompleteness") private var defaultCompleteness = ""
    var body: some View {
        Form {
            Section("Workspace") {
                LabeledContent("Projects root") {
                    Text(store.rootURL.path).font(DS.font.monoInline)
                        .foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
                }
                HStack {
                    Button("Change…") { pickWorkspace() }
                    Button("Reveal in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting([store.rootURL])
                    }
                }
            }
            Section("About") {
                LabeledContent("Orchestrator V2") {
                    Text("Local-first multi-agent app builder.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped).padding()
    }

    private func pickWorkspace() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Use as workspace"
        panel.directoryURL = store.rootURL
        if panel.runModal() == .OK, let url = panel.url {
            store.setWorkspaceRoot(url)
        }
    }
}

// Settings → Local Models: "Local model (Ollama)" (V2 spec §12). Detection +
// guided install, the enable toggle (agents.ollama_enabled), a model picker fed
// from the engine's `--doctor --json` local_models block (installed vs
// needs-pull), a Pull affordance restricted to curated registry entries, and
// the exact §12.4 privacy-boundary text.
struct LocalModelSettings: View {
    @EnvironmentObject var store: OrchestratorStore
    private var info: LocalModelsInfo? { store.localModels }
    private var selected: String { store.agentModels["ollama"] ?? "" }
    @State private var customModelID = ""
    @State private var customLicense = "Apache-2.0"

    // The picker rows: the live doctor registry, else the shipped curated list
    // (shown as not-installed until the first doctor fetch lands).
    private var registry: [LocalModelEntry] {
        if let reg = info?.registry, !reg.isEmpty { return reg }
        return store.recommendedLocalModels.map {
            LocalModelEntry(id: $0.id, label: $0.label, installed: false,
                            license: $0.license, commercialUse: true,
                            minRAMGB: $0.minRAM, recommendedRAMGB: nil,
                            sizeGB: nil, contextTokens: nil, roles: [], notes: "")
        }
    }

    var body: some View {
        ScrollView {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                let ollama = store.cliAvailable["ollama"] ?? false
                Image(systemName: ollama ? "checkmark.circle.fill" : "xmark.circle")
                    .foregroundStyle(ollama ? .green : .orange)
                Text(ollama ? "Ollama detected" : "Ollama not installed")
                if !ollama {
                    Button("Install") {
                        store.runInTerminal("brew install ollama || echo 'Install Homebrew first: https://brew.sh'")
                    }.font(.caption)
                }
                Spacer()
                if let i = info {
                    Text(i.serverRunning ? "server running" : "server not running")
                        .font(.system(size: 10))
                        .foregroundStyle(i.serverRunning ? Color.green : Color.orange)
                    if ollama && !i.serverRunning {
                        Button("Start") { store.startOllamaServer() }
                            .font(.caption)
                            .help("Run ollama serve in Terminal")
                    }
                }
                Button {
                    store.refreshLocalModels()
                } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).font(.caption)
                    .help("Re-check Ollama and installed models")
                    .accessibilityLabel("Refresh local model status")
                Button("Update Installed") {
                    store.updateInstalledLocalModels()
                }
                .font(.caption)
                .disabled(registry.allSatisfy { !$0.installed })
                .help("Run ollama pull again for every installed curated model")
            }

            Toggle(isOn: Binding(
                get: { store.enabledAgents["ollama"] ?? false },
                set: { store.setAgentEnabled("ollama", $0) }
            )) {
                Text("Let local models join runs as teammates").fontWeight(.medium)
            }
            .toggleStyle(.switch)
            Text("They debate, review, and build alongside the cloud agents — and stand in whenever a cloud model hits its limits. A cloud agent always leads while one is available; local models sit out time-boxed sprint runs by default.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("This Mac has \(store.systemMemoryGB)GB of memory. Fit labels are guidance, not a hard limit.")
                .font(.caption2).foregroundStyle(.tertiary)

            Divider()

            HStack(spacing: 8) {
                Text("Model").font(.caption).foregroundStyle(.secondary).frame(width: 44, alignment: .leading)
                Picker("", selection: Binding(
                    get: { selected.isEmpty ? store.defaultModel("ollama") : selected },
                    set: { store.setModel("ollama", $0) }
                )) {
                    ForEach(registry.map(\.id), id: \.self) { id in
                        Text(id).tag(id)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .accessibilityLabel("Default Ollama model")
                Spacer()
            }

            HStack(spacing: 8) {
                TextField("add model tag, e.g. qwen2.5-coder:14b", text: $customModelID)
                    .textFieldStyle(.roundedBorder)
                    .font(.callout)
                Picker("", selection: $customLicense) {
                    Text("Apache-2.0").tag("Apache-2.0")
                    Text("MIT").tag("MIT")
                }
                .labelsHidden()
                .pickerStyle(.menu)
                Button {
                    let added = store.addLocalModel(id: customModelID, license: customLicense)
                    if added { customModelID = "" }
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .disabled(!store.localModelIdIsSafe(customModelID))
            }
            .font(.caption)

            Divider()
            // Model Library: search the wider open-source ecosystem (curated
            // registry + Hugging Face GGUF) and download without leaving the app.
            OpenModelSearchSection()
            Divider()

            ForEach(registry) { m in
                HStack {
                    Button {
                        store.setModel("ollama", m.id)
                    } label: {
                        Image(systemName: selected == m.id ? "largecircle.fill.circle" : "circle")
                            .foregroundStyle(selected == m.id ? Color.accentColor : Color.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Select \(m.id) as the local model")
                    VStack(alignment: .leading, spacing: 1) {
                        Text(m.id).font(.system(size: 12, design: .monospaced))
                        Text(m.label).font(.system(size: 10)).foregroundStyle(.secondary)
                        HStack(spacing: 8) {
                            if !m.license.isEmpty {
                                Label(m.license, systemImage: m.commercialUse ? "checkmark.shield" : "exclamationmark.triangle")
                                    .foregroundStyle(m.commercialUse ? Color.green : Color.orange)
                            }
                            if let min = m.minRAMGB {
                                Text("min \(min)GB RAM")
                            }
                            if let rec = m.recommendedRAMGB {
                                Text("rec \(rec)GB")
                            }
                            if let size = m.sizeGB {
                                Text(String(format: "%.1fGB", size))
                            }
                            if let ctx = m.contextTokens {
                                Text("\(ctx / 1000)K ctx")
                            }
                            Text(m.fitLabel(totalRAMGB: store.systemMemoryGB))
                                .foregroundStyle(fitColor(m.fitLabel(totalRAMGB: store.systemMemoryGB)))
                        }
                        .font(.system(size: 9))
                        .foregroundStyle(.tertiary)
                        if !m.notes.isEmpty {
                            Text(m.notes).font(.system(size: 9)).foregroundStyle(.tertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer()
                    if let p = store.pullProgress[m.id] {
                        if p < 0 {
                            ProgressView().controlSize(.small)
                        } else {
                            ProgressView(value: p).frame(width: 80)
                            Text("\(Int(p * 100))%").font(.system(size: 9)).monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                    } else if m.installed {
                        Toggle("roster", isOn: Binding(
                            get: { store.readLocalRoster().contains(m.id) },
                            set: { store.setRosterMembership(m.id, $0) }
                        ))
                        .toggleStyle(.checkbox).font(.system(size: 10))
                        .help("Include this model as an extra participant in runs (models.ollama_roster)")
                        Label("installed", systemImage: "checkmark.seal.fill")
                            .font(.system(size: 10)).foregroundStyle(.green)
                            .labelStyle(.titleAndIcon)
                        Button {
                            store.deleteLocalModel(m.id)
                        } label: {
                            Image(systemName: "trash")
                        }
                        .buttonStyle(.plain)
                        .help("Delete this local model with ollama rm")
                        .accessibilityLabel("Delete \(m.id)")
                    } else {
                        Text("needs pull").font(.system(size: 10)).foregroundStyle(.orange)
                        Button("Pull") { store.pullModelInApp(m.id) }
                            .font(.caption).disabled(!(store.cliAvailable["ollama"] ?? false))
                    }
                }
                .padding(.vertical, 2)
            }
            if !selected.isEmpty, let i = info, i.selected == selected, !i.selectedInstalled {
                Text("\(selected) is selected but not pulled yet — pull it before running.")
                    .font(.caption2).foregroundStyle(.orange)
            }

            Divider()
            // V2 spec §12.4 privacy boundary — exact wording, do not edit.
            Text("Local-first does not mean every call stays on-device. Codex, Claude, and Gemini calls leave this Mac through their respective CLIs. Ollama-routed local model calls stay on this Mac.")
                .font(.caption2).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding()
        }
        .onAppear { store.refreshLocalModels() }
    }

    private func fitColor(_ label: String) -> Color {
        if label == "good fit" { return .green }
        if label == "tight fit" { return .orange }
        if label == "too large" { return .red }
        return .secondary
    }
}

// MARK: - Usage sheet (agent call counts across projects)

struct UsageSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        let usage = store.usageStats()
        let agents = ["codex", "claude", "gemini"].filter { usage.agentTotals[$0] != nil }
            + usage.agentTotals.keys.filter { !["codex", "claude", "gemini"].contains($0) }.sorted()
        return VStack(alignment: .leading, spacing: 12) {
            Text("Usage").font(.title3).fontWeight(.medium)
            Text("Agent calls per project, from the run logs. (Subscription CLIs don't report tokens, so this counts calls, not tokens or cost.)")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if usage.grandTotal == 0 {
                Text("No runs yet.").font(.caption).foregroundStyle(.tertiary).padding(.vertical, 20)
            } else {
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                    GridRow {
                        Text("Project").font(.caption).foregroundStyle(.secondary)
                        ForEach(agents, id: \.self) { a in
                            Text((Speaker(rawValue: a) ?? .system).display)
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Text("Total").font(.caption).foregroundStyle(.secondary)
                    }
                    Divider()
                    ForEach(usage.rows, id: \.project) { row in
                        GridRow {
                            Text(row.project).font(.system(size: 12))
                            ForEach(agents, id: \.self) { a in
                                Text("\(row.byAgent[a] ?? 0)").font(.system(size: 12, design: .monospaced))
                            }
                            Text("\(row.total)").font(.system(size: 12, weight: .medium, design: .monospaced))
                        }
                    }
                    Divider()
                    GridRow {
                        Text("All").font(.system(size: 12, weight: .medium))
                        ForEach(agents, id: \.self) { a in
                            Text("\(usage.agentTotals[a] ?? 0)").font(.system(size: 12, design: .monospaced))
                        }
                        Text("\(usage.grandTotal)").font(.system(size: 12, weight: .medium, design: .monospaced))
                    }
                }
            }
            HStack { Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.defaultAction) }
        }
        .padding(20).frame(width: 460)
    }
}

// Fleet-level quality-gate controls — shared by Settings › Defaults
// ("Defaults for new projects", per spec §3 secondary surfaces).
struct QualityGatesSection: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var independentFirstRound = true
    @State private var phaseQualityGates = true
    @State private var phaseQualityRepairRounds = 2

    var body: some View {
        Section("Quality gates") {
            Toggle("Independent first round", isOn: Binding(
                get: { independentFirstRound },
                set: {
                    independentFirstRound = $0
                    store.setRuntimeBool("phase_independent_first_round_enabled", $0)
                }))
            Toggle("Quality gate before closing", isOn: Binding(
                get: { phaseQualityGates },
                set: {
                    phaseQualityGates = $0
                    store.setRuntimeBool("phase_quality_gates_enabled", $0)
                }))
            if phaseQualityGates {
                Stepper(value: Binding(
                    get: { phaseQualityRepairRounds },
                    set: {
                        phaseQualityRepairRounds = max(0, $0)
                        store.setRuntimeInt("phase_quality_repair_rounds",
                                            phaseQualityRepairRounds)
                    }), in: 0...10) {
                        Text(phaseQualityRepairRounds == 1
                             ? "1 repair round after failed quality gate"
                             : "\(phaseQualityRepairRounds) repair rounds after failed quality gate")
                    }
            }
            Text("App/spec phases use phase_rules.json as a rubric. A failed quality gate appends evaluator feedback to the transcript and spends repair rounds until the phase passes or hits its cap.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .onAppear {
            independentFirstRound = store.readRuntimeBool(
                "phase_independent_first_round_enabled", defaultValue: true)
            phaseQualityGates = store.readRuntimeBool("phase_quality_gates_enabled",
                                                      defaultValue: true)
            phaseQualityRepairRounds = store.readRuntimeInt(
                "phase_quality_repair_rounds", defaultValue: 2)
        }
    }
}

// Settings › Defaults — "Defaults for new projects": intake defaults, the
// agent team, quality gates, and the fleet routing + fallback ladders (the
// same component the Plan tab embeds per-project from M3).
struct DefaultsSettings: View {
    @EnvironmentObject var store: OrchestratorStore
    @AppStorage("defaultAutonomy") private var defaultAutonomy = "fully_autonomous"
    @AppStorage("defaultCompleteness") private var defaultCompleteness = ""

    var body: some View {
        Form {
            Section("New projects") {
                Picker("Autonomy", selection: $defaultAutonomy) {
                    Text("Fully autonomous").tag("fully_autonomous")
                    Text("Semi-autonomous").tag("semi_autonomous")
                    Text("Manual").tag("manual")
                }
                Picker("Completeness", selection: $defaultCompleteness) {
                    Text("Workflow default").tag("")
                    Text("Prototype").tag("prototype")
                    Text("MVP").tag("mvp")
                    Text("V1").tag("v1")
                    Text("Production draft").tag("production_draft")
                }
            }
            Section("Agent team") {
                ForEach(store.agentOrder, id: \.self) { agent in
                    AgentModelRow(agent: agent)
                }
            }
            QualityGatesSection()
            ModelRoutingSections()
            Section("Routing grid — defaults for new projects") {
                // The same component the Plan tab embeds per-project (§5).
                if let wf = store.workflow(named: "app_build") ?? store.workflows.first {
                    RoutingGridView(scope: .fleet, workflow: wf)
                        .padding(.vertical, 4)
                } else {
                    Text("No workflows found yet — choose a workspace first.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped).padding()
    }
}

// Settings › Advanced — Model Library (local models + open-model search) and
// the seldom-touched editors.
struct AdvancedSettings: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var showSubAgents = false
    @State private var showUsage = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Button("Sub-agents…") { showSubAgents = true }
                Button("Usage…") { showUsage = true }
                Spacer()
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            Divider()
            LocalModelSettings()
        }
        .sheet(isPresented: $showSubAgents) { SubAgentsEditor().environmentObject(store) }
        .sheet(isPresented: $showUsage) { UsageSheet().environmentObject(store) }
    }
}

// Settings › Engine — paths, caps, heartbeats, and the build observer.
struct EngineSettings: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var showRounds = false
    @State private var timeoutSeconds = 1800
    @State private var heartbeatSeconds = 600
    @State private var coordinatorFailover = true
    @State private var maxIntegrationless = 2
    @State private var globalCapEnabled = false

    var body: some View {
        Form {
            Section("Engine") {
                LabeledContent("Orchestrator dir") {
                    Text(store.orchDirURL.path).font(DS.font.monoInline)
                        .foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
                }
            }

            Section("Conversation caps") {
                Button("Edit rounds per phase...") { showRounds = true }
                Text("Rounds are the conversation limit for each workflow phase. Set finite caps for predictable duration, or unlimited (0/∞/infinite) to continue until consensus is accepted.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }

            Section("Concurrency across projects") {
                Toggle("Global worker cap", isOn: Binding(
                    get: { globalCapEnabled },
                    set: { globalCapEnabled = $0; store.setGlobalCapEnabled($0) }))
                Text("Caps total agent workers machine-wide so running many projects at once can't oversubscribe your CPU/RAM or CLI rate limits. Defaults: cli_remote 12, local_model 1 (edit in config.yaml). Fail-open — never blocks a run.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }

            Section("Agent turn timeout") {
                Toggle("Unlimited", isOn: Binding(
                    get: { timeoutSeconds == 0 },
                    set: { on in
                        timeoutSeconds = on ? 0 : 1800
                        store.setRuntimeInt("timeout_seconds_per_agent", timeoutSeconds)
                    }))
                if timeoutSeconds != 0 {
                    Stepper(value: Binding(
                        get: { max(1, timeoutSeconds / 60) },
                        set: {
                            timeoutSeconds = $0 * 60
                            store.setRuntimeInt("timeout_seconds_per_agent", timeoutSeconds)
                        }), in: 1...240) {
                            Text("\(max(1, timeoutSeconds / 60)) min per agent turn")
                        }
                } else {
                    Text("No overall per-agent turn timeout. The no-output guard can still kill silent hangs.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }

            Section("Silent-call guard") {
                Toggle("Kill calls with no output", isOn: Binding(
                    get: { heartbeatSeconds > 0 },
                    set: { on in
                        heartbeatSeconds = on ? 600 : 0
                        store.setRuntimeInt("agent_no_output_heartbeat_seconds", heartbeatSeconds)
                    }))
                if heartbeatSeconds > 0 {
                    Stepper(value: Binding(
                        get: { max(1, heartbeatSeconds / 60) },
                        set: {
                            heartbeatSeconds = $0 * 60
                            store.setRuntimeInt("agent_no_output_heartbeat_seconds", heartbeatSeconds)
                        }), in: 1...120) {
                            Text("\(max(1, heartbeatSeconds / 60)) min with zero output")
                        }
                } else {
                    Text("Disabled. A provider can run silently until its overall timeout, or forever if timeout is also unlimited.")
                        .font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
            }

            Section("Build observer") {
                Toggle("Coordinator failover", isOn: Binding(
                    get: { coordinatorFailover },
                    set: {
                        coordinatorFailover = $0
                        store.setRuntimeBool("coordinator_failover_enabled", $0)
                    }))
                Stepper(value: Binding(
                    get: { maxIntegrationless },
                    set: {
                        maxIntegrationless = max(0, $0)
                        store.setRuntimeInt("max_build_iterations_without_integrator",
                                            maxIntegrationless)
                    }), in: 0...20) {
                        Text(maxIntegrationless == 0
                             ? "No cap on missing integrator"
                             : "\(maxIntegrationless) iteration cap without integrator")
                    }
                Text("When a selected build integrator is hung, unavailable, or in cooldown, the observer promotes another healthy coordinator. If no coordinator can integrate for the cap above, the build stops instead of looping through fragmented worker-only iterations.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .formStyle(.grouped).padding()
        .onAppear {
            timeoutSeconds = store.readRuntimeInt("timeout_seconds_per_agent", defaultValue: 1800)
            heartbeatSeconds = store.readRuntimeInt("agent_no_output_heartbeat_seconds", defaultValue: 600)
            coordinatorFailover = store.readRuntimeBool("coordinator_failover_enabled",
                                                        defaultValue: true)
            maxIntegrationless = store.readRuntimeInt(
                "max_build_iterations_without_integrator", defaultValue: 2)
            globalCapEnabled = store.readGlobalCapEnabled()
        }
        .sheet(isPresented: $showRounds) { RoundsEditor().environmentObject(store) }
    }
}
