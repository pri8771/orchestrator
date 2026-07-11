import SwiftUI
import AppKit

// MARK: - Library › Models & Agents (DESIGN-NATIVE-PRO.md §4.7, milestone M3)
//
// Grouped by provider: agent cards with identity avatar, CLI version (probe),
// default model popup, default EffortPicker (disabled at 40% where the agent
// has no effort control), install status with Fix, and the agent's Fallback
// Ladder — ladders are properties of agents, so they live HERE (per-project
// overrides live on the project's Plan tab). Below: the local-model roster
// with the Active-limit stepper pinned in the section header.

struct ModelsAgentsView: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var routing = ModelRouting()
    @State private var loaded = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.zone) {
                HStack {
                    Text("Models & Agents").font(DS.font.title)
                    Spacer()
                    Button {
                        store.probeAgentVersions()
                        store.refreshLocalModels()
                    } label: {
                        if store.probeAllInFlight {
                            HStack(spacing: DS.space.xxs) {
                                ProgressView().controlSize(.small)
                                Text("Probing…")
                            }
                        } else {
                            Label("Probe All", systemImage: "dot.radiowaves.left.and.right")
                        }
                    }
                    .disabled(store.probeAllInFlight)
                    .help("Ping every agent CLI and the local server; update install badges")
                }

                ForEach(store.agentOrder, id: \.self) { agent in
                    AgentCard(agent: agent, routing: $routing,
                              onRoutingChange: persistRouting)
                }

                localModelsSection
            }
            .padding(DS.space.margin)
        }
        .onAppear {
            if !loaded {
                routing = store.readModelRouting()
                loaded = true
            }
            store.probeAgentVersions()
        }
    }

    private func persistRouting() {
        store.writeModelRouting(routing)
    }

    // MARK: Local Models (roster + active limit)

    private var localModelsSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(spacing: DS.space.xs) {
                Text("Local Models").font(DS.font.headline)
                Spacer()
                ActiveLimitStepper()
            }
            Text("The roster joins runs as extra participants; the active limit caps how many local models load at once.")
                .font(DS.font.caption).foregroundStyle(.secondary)
            LocalRosterList()
            HStack {
                SettingsLink {
                    Label("Open the full Model Library (search + downloads)…",
                          systemImage: "books.vertical")
                        .font(DS.font.caption)
                }
                Spacer()
            }
        }
        .padding(DS.space.s)
        .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .fill(.quaternary.opacity(0.5)))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .stroke(DS.hairline, lineWidth: 1))
    }
}

// MARK: - One agent card (64pt rows + folded ladder)

private struct AgentCard: View {
    @EnvironmentObject var store: OrchestratorStore
    let agent: String
    @Binding var routing: ModelRouting
    let onRoutingChange: () -> Void

    @State private var ladderExpanded = false

    private var identity: DS.AgentIdentity { DS.identity(agent) }
    private var available: Bool { store.cliAvailable[agent] ?? false }
    private var enabled: Bool { store.enabledAgents[agent] ?? false }
    private var chain: [String] { routing.chains[agent] ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            headerRow
            modelRow
            effortRow
            if agent != "ollama" {
                ladder
            }
        }
        .padding(DS.space.s)
        .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .fill(DS.cardBg))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .stroke(DS.hairline, lineWidth: 1))
        .opacity(enabled ? 1 : 0.55)
    }

    private var headerRow: some View {
        HStack(spacing: DS.space.xs) {
            AgentAvatar(identity: identity, size: 28)
            VStack(alignment: .leading, spacing: 1) {
                Text(identity.displayName).font(DS.font.headline)
                if let v = store.cliVersions[agent] {
                    Text(v)
                        .font(DS.font.monoInline)
                        .foregroundStyle(DS.textSecondary)
                        .lineLimit(1)
                } else {
                    Text(available ? "version unknown — Probe All" : "CLI not installed")
                        .font(DS.font.caption)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer()
            if !available {
                Button("Fix…") { fixInstall() }
                    .font(DS.font.caption)
                    .help(fixHelp)
                StatusPill(kind: .warning, label: "Not found")
            } else {
                StatusPill(kind: .success, label: "Installed")
            }
            Toggle("", isOn: Binding(
                get: { enabled },
                set: { store.setAgentEnabled(agent, $0) }
            ))
            .toggleStyle(.switch)
            .controlSize(.small)
            .labelsHidden()
            .accessibilityLabel("\(identity.displayName) takes part in runs")
        }
    }

    private var modelRow: some View {
        HStack(spacing: DS.space.xs) {
            Text("Default model")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .frame(width: 90, alignment: .leading)
            Picker("", selection: Binding(
                get: { store.agentModels[agent] ?? store.defaultModel(agent) },
                set: { store.setModel(agent, $0) }
            )) {
                ForEach(store.modelPresets(agent), id: \.self) { m in
                    Text(m).font(DS.font.monoInline).tag(m)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(maxWidth: 280, alignment: .leading)
            .accessibilityLabel("Default model for \(identity.displayName)")
            Spacer()
        }
    }

    @ViewBuilder
    private var effortRow: some View {
        HStack(alignment: .top, spacing: DS.space.xs) {
            Text("Default effort")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .frame(width: 90, alignment: .leading)
            EffortPicker(level: Binding(
                get: { EffortLevel(configValue: store.agentEfforts[agent] ?? "") },
                set: { level in
                    guard identity.supportsEffort else { return }
                    store.setReasoningEffort(agent, level.map(\.configValue) ?? "medium")
                }
            ), agent: agent, showFootnote: identity.supportsEffort)
            Spacer()
        }
    }

    // MARK: Fallback ladder (drag-reorderable rungs)

    private var ladder: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Button {
                withAnimation(DS.spring) { ladderExpanded.toggle() }
            } label: {
                HStack(spacing: DS.space.xxs) {
                    Image(systemName: ladderExpanded ? "chevron.down" : "chevron.right")
                        .font(DS.font.caption)
                    Label("Fallback Ladder", systemImage: "link")
                        .font(DS.font.callout)
                    if !chain.isEmpty {
                        Text("\(chain.count) rung\(chain.count == 1 ? "" : "s")")
                            .font(DS.font.caption).foregroundStyle(.secondary)
                    } else {
                        Text("safety net only")
                            .font(DS.font.caption).foregroundStyle(.tertiary)
                    }
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Fallback ladder for \(identity.displayName), \(chain.count) rungs")

            if ladderExpanded {
                LadderEditor(agent: agent, chain: Binding(
                    get: { routing.chains[agent] ?? [] },
                    set: {
                        routing.chains[agent] = $0.isEmpty ? nil : $0
                        onRoutingChange()
                    }))
            }
        }
    }

    private var fixHelp: String {
        switch agent {
        case "codex": return "npm install -g @openai/codex"
        case "claude": return "npm install -g @anthropic-ai/claude-code"
        case "gemini": return "npm install -g @google/gemini-cli"
        default: return "brew install ollama"
        }
    }

    private func fixInstall() {
        let cmd: String
        switch agent {
        case "codex": cmd = "npm install -g @openai/codex"
        case "claude": cmd = "npm install -g @anthropic-ai/claude-code"
        case "gemini": cmd = "npm install -g @google/gemini-cli"
        default: cmd = "brew install ollama || echo 'Install Homebrew first: https://brew.sh'"
        }
        store.runInTerminal(cmd)
    }
}

private extension EffortLevel {
    var configValue: String {
        switch self {
        case .low: return "low"
        case .medium: return "medium"
        case .high: return "high"
        }
    }
}

// MARK: - Ladder editor: drag-reorderable rungs with rank, mono model, size
// captions on local rungs, and a small-model review warning.

private struct LadderEditor: View {
    @EnvironmentObject var store: OrchestratorStore
    let agent: String
    @Binding var chain: [String]

    private var installedLocal: [String] {
        (store.localModels?.registry ?? []).filter(\.installed).map(\.id)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            if hasSmallReviewRung {
                HStack(spacing: DS.space.xxs) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(DS.status.warning.color)
                    Text("Small models produce low-quality reviews — a model under 7B sits on this ladder.")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.status.warning.color)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(DS.space.xxs)
                .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                    .fill(DS.status.warning.fill))
            }

            List {
                ForEach(Array(chain.enumerated()), id: \.element) { i, step in
                    rungRow(rank: i + 1, step: step)
                        .listRowSeparator(.hidden)
                        .listRowInsets(EdgeInsets(top: 2, leading: 0, bottom: 2, trailing: 0))
                }
                .onMove { from, to in
                    chain.move(fromOffsets: from, toOffset: to)
                }
                .onDelete { chain.remove(atOffsets: $0) }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .frame(height: max(30, CGFloat(chain.count) * 30))

            addRungMenu

            Text("Tried top to bottom when \(DS.identity(agent).displayName) fails mid-run; the local safety net always catches last. Drag to reorder.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var hasSmallReviewRung: Bool {
        chain.contains { step in
            let tag = step.hasPrefix("local:") ? String(step.dropFirst(6)) : step
            return (RoutingConsequences.parameterBillions(tag) ?? 99) < 7
        }
    }

    private func rungRow(rank: Int, step: String) -> some View {
        let isLocal = step.hasPrefix("local:")
        let tag = isLocal ? String(step.dropFirst(6)) : step
        let size = RoutingConsequences.parameterBillions(tag)
        return HStack(spacing: DS.space.xs) {
            Text("\(rank)")
                .font(DS.font.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 14)
            Image(systemName: "line.3.horizontal")
                .font(DS.font.caption)
                .foregroundStyle(.tertiary)
                .accessibilityHidden(true)
            Text(tag)
                .font(DS.font.monoInline)
                .lineLimit(1).truncationMode(.middle)
            if isLocal {
                Text(size.map { "local · \(Int($0))B" } ?? "local")
                    .font(DS.font.caption)
                    .foregroundStyle(size.map { $0 < 7 } == true
                                     ? DS.status.warning.color : DS.textTertiary)
            }
            Spacer()
            Button {
                chain.removeAll { $0 == step }
            } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove rung \(tag)")
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Rung \(rank): \(tag)\(isLocal ? ", local" : "")")
    }

    private var addRungMenu: some View {
        Menu {
            Section("Same provider, different model") {
                ForEach(store.modelPresets(agent).filter { !chain.contains($0) },
                        id: \.self) { m in
                    Button(m) { chain.append(m) }
                }
            }
            Section("Local, on this Mac") {
                ForEach(installedLocal.filter { !chain.contains("local:\($0)") },
                        id: \.self) { m in
                    Button(m) { chain.append("local:\(m)") }
                }
            }
        } label: {
            Label("Add rung", systemImage: "plus.circle")
                .font(DS.font.caption)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .disabled(chain.count >= 5)   // engine caps chains at 5 steps
        .accessibilityLabel("Add fallback rung for \(DS.identity(agent).displayName)")
    }
}

// MARK: - Active limit stepper (config models.local_active_limit)

private struct ActiveLimitStepper: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var limit = 4

    var body: some View {
        HStack(spacing: DS.space.xxs) {
            Text("Active limit")
                .font(DS.font.caption).foregroundStyle(.secondary)
            Stepper(value: Binding(
                get: { limit },
                set: {
                    limit = max(1, min(9, $0))
                    store.setRuntimeInt("local_active_limit", limit)
                }), in: 1...9) {
                Text("\(limit)")
                    .font(DS.font.callout).monospacedDigit()
            }
            .controlSize(.small)
        }
        .onAppear { limit = store.readRuntimeInt("local_active_limit", defaultValue: 4) }
        .help("How many local models may participate at once (models.local_active_limit)")
        .accessibilityLabel("Local active limit: \(limit)")
    }
}

// MARK: - Roster list (installed local models with enable toggles + size badges)

private struct LocalRosterList: View {
    @EnvironmentObject var store: OrchestratorStore

    private var registry: [LocalModelEntry] {
        (store.localModels?.registry ?? []).filter(\.installed)
    }

    var body: some View {
        if registry.isEmpty {
            Text("No local models installed yet — pull one from the Model Library.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
        } else {
            ForEach(registry) { m in
                HStack(spacing: DS.space.xs) {
                    Toggle("", isOn: Binding(
                        get: { store.readLocalRoster().contains(m.id) },
                        set: { store.setRosterMembership(m.id, $0) }
                    ))
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .labelsHidden()
                    .accessibilityLabel("Include \(m.id) in the roster")
                    Text(m.id)
                        .font(DS.font.monoInline)
                        .lineLimit(1).truncationMode(.middle)
                    if let size = m.sizeGB {
                        Text(String(format: "%.1fGB", size))
                            .font(DS.font.caption).foregroundStyle(.tertiary)
                    }
                    if let b = RoutingConsequences.parameterBillions(m.id) {
                        Text("\(Int(b))B")
                            .font(DS.font.caption)
                            .foregroundStyle(b < 7 ? DS.status.warning.color : DS.textTertiary)
                    }
                    Spacer()
                }
            }
        }
    }
}
