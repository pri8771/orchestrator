import SwiftUI
import AppKit

// MARK: - Project inspector (DESIGN-NATIVE-PRO.md §3 region 3, milestone M2)
//
// Context-sensitive, Xcode-style icon tabs:
//   1. Plan Summary — one compact row per phase with the models/effort that
//      phase resolves to, provenance + Revert. (Becomes the persistent cell
//      editor when a grid cell is selected — M3.)
//   2. Agents — participation toggles, per-agent ladder summary, live
//      planned→actual status (actual arrives with M4's events.jsonl).
//   3. Info — idea prompt, attached docs, dates, workspace path.
//
// Reads/writes the SAME files the legacy Settings tabs used, through existing
// store methods: config.yaml (setAgentEnabled), model_routing.json
// (read/writeModelRouting).

enum InspectorTab: String, CaseIterable, Identifiable {
    case planSummary, agents, info
    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .planSummary: return "slider.horizontal.3"
        case .agents: return "person.2.badge.gearshape"
        case .info: return "info.circle"
        }
    }
    var label: String {
        switch self {
        case .planSummary: return "Plan Summary"
        case .agents: return "Agents"
        case .info: return "Info"
        }
    }
}

struct ProjectInspectorView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project

    @State private var tab: InspectorTab = .planSummary
    @State private var routing = ModelRouting()          // fleet routing (base)
    @State private var projectRouting = ModelRouting()   // per-project overrides

    var body: some View {
        VStack(spacing: 0) {
            Picker("Inspector tab", selection: $tab) {
                ForEach(InspectorTab.allCases) { t in
                    Image(systemName: t.symbol)
                        .accessibilityLabel(t.label)
                        .tag(t)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(DS.space.xs)
            Divider()
            ScrollView {
                switch tab {
                case .planSummary:
                    PlanSummaryTab(project: project, fleet: $routing,
                                   projectRouting: $projectRouting)
                case .agents:
                    AgentsTab(project: project,
                              routing: mergedChains(fleet: routing, project: projectRouting))
                case .info:
                    InfoTab(project: project)
                }
            }
        }
        .onAppear(perform: reload)
        .onChange(of: project.name) { _, _ in reload() }
        // A grid cell selection makes this tab the persistent editor (§5.3).
        .onChange(of: store.planCellSelection) { _, sel in
            if let sel, sel.project == project.name { tab = .planSummary }
        }
    }

    private func reload() {
        routing = store.readModelRouting()
        projectRouting = store.readProjectRouting(project)
    }

    // Ladder summary: project chains override fleet chains where present.
    private func mergedChains(fleet: ModelRouting, project p: ModelRouting) -> ModelRouting {
        var merged = fleet
        for (agent, chain) in p.chains where !chain.isEmpty {
            merged.chains[agent] = chain
        }
        return merged
    }
}

// MARK: - Tab 1: Plan Summary

private struct PlanSummaryTab: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    @Binding var fleet: ModelRouting
    @Binding var projectRouting: ModelRouting

    private var phases: [PhaseDef] { store.phases(for: project) }

    // Per-project resolution: the project's model_routing.json over the fleet
    // file over config defaults — the same RoutingMatrix the Plan grid uses.
    private var matrix: RoutingMatrix {
        RoutingMatrix(scope: .project(name: project.name),
                      draft: projectRouting,
                      base: fleet,
                      defaults: store.agentModels,
                      defaultEfforts: store.agentEfforts,
                      enabledAgents: store.enabledAgents,
                      installedLocal: Set((store.localModels?.registry ?? [])
                          .filter(\.installed).map(\.id)))
    }

    // Phase keys of THIS workflow that carry a project-level override.
    private var overriddenKeys: [String] {
        phases.map(\.key).filter { !(projectRouting.phases[$0] ?? PhaseRoute()).isEmpty }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            provenance
            if let sel = store.planCellSelection, sel.project == project.name,
               phases.contains(where: { $0.key == sel.phaseKey }) {
                Divider()
                cellEditor(sel)
            }
            Divider()
            ForEach(phases) { phase in
                phaseRow(phase)
            }
            Divider()
            Button {
                store.uiCommand = .openPlanTab
            } label: {
                Label("Open Plan tab", systemImage: "slider.horizontal.3")
                    .font(DS.font.caption)
            }
        }
        .padding(DS.space.s)
    }

    @ViewBuilder
    private var provenance: some View {
        let n = overriddenKeys.count
        if n == 0 {
            Text("Inherited from fleet defaults")
                .font(DS.font.caption).foregroundStyle(.secondary)
        } else {
            HStack(spacing: DS.space.xxs) {
                Circle().fill(DS.accent.color).frame(width: 5, height: 5)
                    .accessibilityHidden(true)
                Text("Customized · \(n) override\(n == 1 ? "" : "s")")
                    .font(DS.font.caption).foregroundStyle(.secondary)
                Button("Revert") { revertAll() }
                    .font(DS.font.caption)
                    .buttonStyle(.plain)
                    .foregroundStyle(DS.accent.color)
                    .accessibilityLabel("Revert this project's routing overrides")
            }
        }
    }

    private func revertAll() {
        for key in overriddenKeys { projectRouting.phases[key] = nil }
        store.writeProjectRouting(projectRouting, for: project)
    }

    // The persistent cell editor (§5.3): the Plan grid's single-click
    // selection lands here, non-modal.
    @ViewBuilder
    private func cellEditor(_ sel: OrchestratorStore.PlanCellSelection) -> some View {
        let identity = DS.identity(sel.agent)
        let cell = matrix.cell(sel.phaseKey, sel.agent)
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(spacing: DS.space.xxs) {
                AgentAvatar(identity: identity, size: 16)
                Text("\(project.titleFor(sel.phaseKey)) · \(identity.displayName)")
                    .font(DS.font.callout)
                Spacer()
                if cell.overriddenHere {
                    Button("Revert") { editCell(sel) { $0.revertCell(phase: sel.phaseKey, agent: sel.agent) } }
                        .font(DS.font.caption)
                }
            }
            Picker("Model", selection: Binding(
                get: { projectRouting.phases[sel.phaseKey]?.model(for: sel.agent) ?? "" },
                set: { model in editCell(sel) { $0.setModel(model, phase: sel.phaseKey, agent: sel.agent) } }
            )) {
                Text("Inherit (\(cell.model.isEmpty ? "default" : cell.model))").tag("")
                ForEach(modelOptions(sel.agent), id: \.self) { m in
                    Text(m).font(DS.font.monoInline).tag(m)
                }
            }
            .pickerStyle(.menu)
            .font(DS.font.caption)
            EffortPicker(level: Binding(
                get: { cell.effort },
                set: { level in editCell(sel) { $0.setEffort(level, phase: sel.phaseKey, agent: sel.agent) } }
            ), agent: sel.agent, showFootnote: false)
        }
        .padding(DS.space.xs)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .fill(DS.accent.fill))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .stroke(DS.accent.stroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Editing \(project.titleFor(sel.phaseKey)), \(identity.displayName)")
    }

    private func modelOptions(_ agent: String) -> [String] {
        agent == "ollama"
            ? (store.localModels?.registry ?? []).filter(\.installed).map(\.id)
            : store.modelPresets(agent)
    }

    // Inspector edits write straight to the project's file; the Plan grid
    // follows the file when clean (its file-changed banner guards dirty state).
    private func editCell(_ sel: OrchestratorStore.PlanCellSelection,
                          _ body: (inout RoutingMatrix) -> Void) {
        var m = matrix
        body(&m)
        projectRouting = m.draft
        store.writeProjectRouting(projectRouting, for: project)
    }

    private func phaseRow(_ phase: PhaseDef) -> some View {
        let overridden = !(projectRouting.phases[phase.key] ?? PhaseRoute()).isEmpty
        return VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xxs) {
                Text(phase.title)
                    .font(DS.font.callout)
                    .lineLimit(1)
                if overridden {
                    Circle().fill(DS.accent.color).frame(width: 5, height: 5)
                        .help("Overridden — Revert on the provenance line")
                        .accessibilityLabel("Overridden")
                }
                Spacer()
                Text("× \(phase.rounds == 0 ? "∞" : String(phase.rounds))")
                    .font(DS.font.caption).monospacedDigit()
                    .foregroundStyle(.tertiary)
            }
            HStack(spacing: DS.space.xs) {
                agentCell(phase.key, "claude")
                agentCell(phase.key, "codex")
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private func agentCell(_ phaseKey: String, _ agent: String) -> some View {
        if store.enabledAgents[agent] ?? false {
            let id = DS.identity(agent)
            let cell = matrix.cell(phaseKey, agent)
            HStack(spacing: DS.space.xxs) {
                AgentAvatar(identity: id, size: 14)
                Text(shortModel(cell.model.isEmpty ? "default" : cell.model))
                    .font(DS.font.monoInline)
                    .foregroundStyle(cell.overriddenHere ? DS.textPrimary : DS.textSecondary)
                    .lineLimit(1)
                if id.supportsEffort, let level = cell.effort {
                    EffortGauge(level: level, tint: id.tint)
                }
            }
        }
    }

    private func shortModel(_ id: String) -> String {
        // "claude-opus-4-8" → readable short name; full ids belong to pickers.
        id.count > 22 ? String(id.prefix(20)) + "…" : id
    }
}

// MARK: - Tab 2: Agents

private struct AgentsTab: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let routing: ModelRouting

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Participation is fleet-wide (config.yaml); per-phase overrides live on the Plan tab.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(store.agentOrder, id: \.self) { agent in
                agentCard(agent)
            }
        }
        .padding(DS.space.s)
    }

    private func agentCard(_ agent: String) -> some View {
        let id = DS.identity(agent)
        let available = store.cliAvailable[agent] ?? false
        let planned = store.agentModels[agent] ?? ""
        let liveNow = project.running
            && (project.nextAgent?.hasPrefix(agent) ?? false)
        return VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xs) {
                AgentAvatar(identity: id, size: 20)
                Toggle(isOn: Binding(
                    get: { store.enabledAgents[agent] ?? false },
                    set: { store.setAgentEnabled(agent, $0) }
                )) {
                    Text(id.displayName).font(DS.font.callout)
                }
                .toggleStyle(.switch)
                .controlSize(.mini)
                Spacer()
                if liveNow {
                    StatusPill(kind: .running, label: "Running", breathing: true)
                } else if !available {
                    StatusPill(kind: .warning, label: "CLI not found")
                }
            }
            // Planned→actual route line (11pt-mono diff). The "actual" side is
            // fed by events.jsonl in M4; until then intent == plan.
            Text(planned.isEmpty ? "—" : planned)
                .font(DS.font.monoInline)
                .foregroundStyle(DS.textSecondary)
                .lineLimit(1)
                .accessibilityLabel("Planned model \(planned)")
            ladderSummary(agent)
        }
        .padding(DS.space.xs)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .fill(.quaternary.opacity(0.5)))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .stroke(DS.hairline, lineWidth: 1))
    }

    @ViewBuilder
    private func ladderSummary(_ agent: String) -> some View {
        let chain = routing.chains[agent] ?? []
        HStack(spacing: DS.space.xxs) {
            Image(systemName: "link")
                .font(DS.font.caption)
                .foregroundStyle(DS.textTertiary)
                .accessibilityHidden(true)
            if chain.isEmpty {
                Text(routing.cloudToLocal ? "Local safety net only" : "No fallback ladder")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            } else {
                Text(chain.joined(separator: " → "))
                    .font(DS.font.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Fallback ladder: \(chain.isEmpty ? "local safety net" : chain.joined(separator: ", then "))")
    }
}

// MARK: - Tab 3: Info

private struct InfoTab: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    @State private var prompt = ""
    @State private var docs: [URL] = []

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Idea prompt").font(DS.font.callout).foregroundStyle(.secondary)
            Text(prompt.isEmpty ? "—" : prompt)
                .font(DS.font.body)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(DS.space.xs)
                .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                    .fill(.quaternary.opacity(0.5)))

            if !docs.isEmpty {
                Text("Attached docs").font(DS.font.callout).foregroundStyle(.secondary)
                ForEach(docs, id: \.self) { url in
                    Button {
                        NSWorkspace.shared.open(url)
                    } label: {
                        HStack(spacing: DS.space.xs) {
                            Image(systemName: "doc.text")
                                .foregroundStyle(DS.accent.color)
                            Text(url.lastPathComponent)
                                .font(DS.font.caption)
                                .lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Image(systemName: "eye")
                                .font(DS.font.caption)
                                .foregroundStyle(.tertiary)
                        }
                        .frame(height: 32)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Preview \(url.lastPathComponent)")
                }
            }

            Divider()
            metadataRow("Workflow", project.workflowTitle)
            if let created = creationDate {
                metadataRow("Created", created.formatted(date: .abbreviated, time: .shortened))
            }
            if let last = project.lastProcessed {
                metadataRow("Last activity", last)
            }
            Text("Workspace").font(DS.font.callout).foregroundStyle(.secondary)
            Text(project.dirURL.path)
                .font(DS.font.monoInline)
                .foregroundStyle(DS.textSecondary)
                .lineLimit(2)
                .truncationMode(.head)
                .textSelection(.enabled)
            Button {
                NSWorkspace.shared.activateFileViewerSelecting([project.dirURL])
            } label: {
                Label("Reveal in Finder", systemImage: "folder")
                    .font(DS.font.caption)
            }
        }
        .padding(DS.space.s)
        .task(id: project.name) {
            prompt = store.initialPrompt(for: project)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            docs = Self.attachedDocs(in: project.dirURL)
        }
    }

    private var creationDate: Date? {
        (try? FileManager.default.attributesOfItem(atPath: project.dirURL.path))?[.creationDate]
            as? Date
    }

    private func metadataRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(DS.font.caption).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(DS.font.caption)
                .multilineTextAlignment(.trailing)
        }
        .accessibilityElement(children: .combine)
    }

    static func attachedDocs(in dir: URL) -> [URL] {
        let docsDir = dir.appendingPathComponent("docs", isDirectory: true)
        guard let items = try? FileManager.default.contentsOfDirectory(
            at: docsDir, includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]) else { return [] }
        return items.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
}
